from collections import OrderedDict

import torch
import torch.nn as nn
import numpy as np

from utils.tools import get_mask_from_lengths, pad, word_level_pooling

from .blocks import (
    ConvNorm,
    FFTBlock,
    MultiHeadAttention,
    RelativeFFTBlock,
)
from text.symbols import symbols


def get_sinusoid_encoding_table(n_position, d_hid, padding_idx=None):
    """ Sinusoid position encoding table """

    def cal_angle(position, hid_idx):
        return position / np.power(10000, 2 * (hid_idx // 2) / d_hid)

    def get_posi_angle_vec(position):
        return [cal_angle(position, hid_j) for hid_j in range(d_hid)]

    sinusoid_table = np.array(
        [get_posi_angle_vec(pos_i) for pos_i in range(n_position)]
    )

    sinusoid_table[:, 0::2] = np.sin(sinusoid_table[:, 0::2])  # dim 2i
    sinusoid_table[:, 1::2] = np.cos(sinusoid_table[:, 1::2])  # dim 2i+1

    if padding_idx is not None:
        # zero vector for padding dimension
        sinusoid_table[padding_idx] = 0.0

    return torch.FloatTensor(sinusoid_table)


class LinguisticEncoder(nn.Module):
    """ Linguistic Encoder """

    def __init__(self, config, abs_mha=True):
        super(LinguisticEncoder, self).__init__()

        n_position = config["max_seq_len"] + 1
        n_src_vocab = len(symbols) + 1
        d_word_vec = config["transformer"]["encoder_hidden"]
        n_layers = config["transformer"]["encoder_layer"]
        n_head = config["transformer"]["encoder_head"]
        d_k = d_v = (
            config["transformer"]["encoder_hidden"]
            // config["transformer"]["encoder_head"]
        )
        d_model = config["transformer"]["encoder_hidden"]
        d_inner = config["transformer"]["conv_filter_size"]
        kernel_size = config["transformer"]["conv_kernel_size"]
        # dropout = config["transformer"]["encoder_dropout"]
        window_size = config["transformer"]["encoder_window_size"]

        self.max_seq_len = config["max_seq_len"]
        self.d_model = d_model
        self.n_head = n_head

        self.src_emb = nn.Embedding(
            n_src_vocab, d_word_vec, padding_idx=0
        )
        self.abs_position_enc = nn.Parameter(
            get_sinusoid_encoding_table(n_position, d_word_vec).unsqueeze(0),
            requires_grad=False,
        )
        self.kv_position_enc = nn.Parameter(
            get_sinusoid_encoding_table(n_position, d_word_vec).unsqueeze(0),
            requires_grad=True,
        )
        self.q_position_enc = nn.Parameter(
            get_sinusoid_encoding_table(n_position, d_word_vec).unsqueeze(0),
            requires_grad=True,
        )

        self.abs_mha = abs_mha
        if abs_mha:
            self.phoneme_encoder = nn.ModuleList(
                [
                    FFTBlock(
                        d_model, n_head, d_k, d_v, d_inner, kernel_size  # , dropout=dropout
                    )
                    for _ in range(n_layers)
                ]
            )
            self.word_encoder = nn.ModuleList(
                [
                    FFTBlock(
                        d_model, n_head, d_k, d_v, d_inner, kernel_size  # , dropout=dropout
                    )
                    for _ in range(n_layers)
                ]
            )
        else:
            self.phoneme_encoder = RelativeFFTBlock(
                d_model,
                d_inner,
                n_head,
                n_layers,
                kernel_size,
                # dropout,
                window_size,
            )
            self.word_encoder = RelativeFFTBlock(
                d_model,
                d_inner,
                n_head,
                n_layers,
                kernel_size,
                # dropout,
                window_size,
            )
        self.length_regulator = LengthRegulator()
        self.duration_predictor = VariancePredictor(config)

        self.w2p_attn = MultiHeadAttention(
            n_head, d_model, d_k, d_v  # , dropout=dropout
        )

    def get_mapping_mask(self, q, kv, dur_w, wb, src_w_len):
        """
        A word-to-phoneme mapping mask to the attention weight to force each query (Q) 
        to only attend to the phonemes belongs to the word corresponding to this query.
        """
        batch_size, q_len, kv_len, device = q.shape[0], q.shape[1], kv.shape[1], kv.device
        mask = torch.ones(batch_size, q_len, kv_len, device=device)
        for b, (w, p, l) in enumerate(zip(dur_w, wb, src_w_len)):
            w, p = [0]+[d.item() for d in torch.cumsum(w[:l], dim=0)], [0] + \
                [d.item() for d in torch.cumsum(p[:l], dim=0)]
            # assert len(w) == len(p)
            for i in range(1, len(w)):
                mask[b, w[i-1]:w[i], p[i-1]:p[i]
                     ] = torch.zeros(w[i]-w[i-1], p[i]-p[i-1], device=device)
        return mask == 1.

    def add_position_enc(self, src_seq, position_enc=None, coef=None):
        batch_size, max_len = src_seq.shape[0], src_seq.shape[1]
        if not self.training and src_seq.shape[1] > self.max_seq_len:
            pos_enc = get_sinusoid_encoding_table(
                src_seq.shape[1], self.d_model
            )[: src_seq.shape[1], :].unsqueeze(0).expand(batch_size, -1, -1).to(
                src_seq.device
            )
            if coef is not None:
                pos_enc = coef.unsqueeze(-1) * pos_enc
            enc_output = src_seq + pos_enc
        else:
            position_enc = self.abs_position_enc if position_enc is None else position_enc
            pos_enc = position_enc[
                :, :max_len, :
            ].expand(batch_size, -1, -1)
            if coef is not None:
                pos_enc = coef.unsqueeze(-1) * pos_enc
            enc_output = src_seq + pos_enc
        return enc_output

    def get_rel_coef(self, dur, dur_len, mask):
        """
        A well-designed positional encoding to the inputs of word-to-phoneme attention module.
        """
        idx, L, device = [], [], dur.device
        for d, dl in zip(dur, dur_len):
            idx_b, d = [], d[:dl].long()
            m = torch.repeat_interleave(d, torch.tensor(
                list(d), device=device), dim=0)  # [tgt_len]
            L.append(m)
            for d_i in d:
                idx_b += list(range(d_i))
            idx.append(torch.tensor(idx_b).to(device))
            # assert L[-1].shape == idx[-1].shape
        return torch.div(pad(idx).to(device), pad(L).masked_fill(mask, 1.).to(device))

    def encode(self, encoder, src_seq, mask, src_emb=True, return_attns=False):
        slf_attn_list = []
        slf_attn_mask = mask.unsqueeze(1).expand(-1, src_seq.shape[1], -1)
        if src_emb:
            src_seq = self.src_emb(src_seq)
        # Will be replaced with Relative PE
        enc_out = self.add_position_enc(src_seq)
        for enc_layer in encoder:
            enc_out, enc_slf_attn = enc_layer(
                enc_out, mask=mask, slf_attn_mask=slf_attn_mask
            )
            if return_attns:
                slf_attn_list += [enc_slf_attn]
        return enc_out, slf_attn_list

    def forward(
        self,
        src_seq,
        src_len,
        wb,
        p_mask,
        src_w_len,
        w_mask,
        mel_mask=None,
        max_len=None,
        duration_target=None,
        d_control=1.0,
        return_attns=False,
    ):
        # Phoneme Encoding
        if self.abs_mha:
            enc_out_p, slf_attn_list_p = self.encode(
                self.phoneme_encoder, src_seq, p_mask, return_attns=return_attns)
        else:
            src_seq = self.src_emb(src_seq)
            enc_out_p = self.phoneme_encoder(src_seq.transpose(
                1, 2), p_mask.unsqueeze(1)).transpose(1, 2)

        # Word-level Pooing
        src_seq_w = word_level_pooling(
            enc_out_p, src_len, wb, src_w_len, reduce_mean=True)

        # Word Encoding
        if self.abs_mha:
            enc_out_w, slf_attn_list_w = self.encode(
                self.word_encoder, src_seq_w, w_mask, src_emb=False, return_attns=return_attns)
        else:
            enc_out_w = self.word_encoder(src_seq_w.transpose(
                1, 2), w_mask.unsqueeze(1)).transpose(1, 2)

        # Phoneme-level Duration Prediction
        log_duration_p_prediction = self.duration_predictor(enc_out_p, p_mask)

        # Word-level Pooling
        log_duration_w_prediction = word_level_pooling(
            log_duration_p_prediction.unsqueeze(-1), src_len, wb, src_w_len, reduce_sum=True).squeeze(-1)

        x = enc_out_w
        if duration_target is not None:
            # Word-level Pooing
            duration_w_rounded = word_level_pooling(
                duration_target.unsqueeze(-1), src_len, wb, src_w_len, reduce_sum=True).squeeze(-1)
            # Word-level Length Regulate
            x, mel_len = self.length_regulator(x, duration_w_rounded, max_len)
        else:
            # Word-level Duration
            duration_w_rounded = torch.clamp(
                (torch.round(torch.exp(log_duration_w_prediction) - 1) * d_control),
                min=0,
            ).long()
            # Word-level Length Regulate
            x, mel_len = self.length_regulator(x, duration_w_rounded, max_len)
            mel_mask = get_mask_from_lengths(mel_len)

        # Word-to-Phoneme Attention
        # [batch, mel_len, seq_len]
        src_mask_ = p_mask.unsqueeze(1).expand(-1, mel_mask.shape[1], -1)
        # [batch, mel_len, seq_len]
        mel_mask_ = mel_mask.unsqueeze(-1).expand(-1, -1, p_mask.shape[1])
        mapping_mask = self.get_mapping_mask(
            x, enc_out_p, duration_w_rounded, wb, src_w_len)  # [batch, mel_len, seq_len]

        q = self.add_position_enc(x, position_enc=self.q_position_enc, coef=self.get_rel_coef(
            duration_w_rounded, src_w_len, mel_mask))
        k = self.add_position_enc(
            enc_out_p, position_enc=self.kv_position_enc, coef=self.get_rel_coef(wb, src_len, p_mask))
        v = self.add_position_enc(
            enc_out_p, position_enc=self.kv_position_enc, coef=self.get_rel_coef(wb, src_len, p_mask))
        # q = self.add_position_enc(x)
        # k = self.add_position_enc(enc_out_p)
        # v = self.add_position_enc(enc_out_p)
        x, alignment = self.w2p_attn(
            q, k, v, mask_1=src_mask_, mask_2=mel_mask_, mapping_mask=mapping_mask, indivisual_attn=True
        )

        return (
            x,
            log_duration_w_prediction,
            duration_w_rounded,
            mel_len,
            mel_mask,
            alignment,
        )


class LengthRegulator(nn.Module):
    """ Length Regulator """

    def __init__(self):
        super(LengthRegulator, self).__init__()

    def LR(self, x, duration, max_len):
        output = list()
        mel_len = list()
        for batch, expand_target in zip(x, duration):
            expanded = self.expand(batch, expand_target)
            output.append(expanded)
            mel_len.append(expanded.shape[0])

        if max_len is not None:
            output = pad(output, max_len)
        else:
            output = pad(output)

        return output, torch.LongTensor(mel_len).to(x.device)

    def expand(self, batch, predicted):
        out = list()

        for i, vec in enumerate(batch):
            expand_size = predicted[i].item()
            out.append(vec.expand(max(int(expand_size), 0), -1))
        out = torch.cat(out, 0)

        return out

    def forward(self, x, duration, max_len):
        output, mel_len = self.LR(x, duration, max_len)
        return output, mel_len


class VariancePredictor(nn.Module):
    """ Duration, Pitch and Energy Predictor """

    def __init__(self, model_config):
        super(VariancePredictor, self).__init__()

        self.input_size = model_config["transformer"]["encoder_hidden"]
        self.filter_size = model_config["variance_predictor"]["filter_size"]
        self.kernel = model_config["variance_predictor"]["kernel_size"]
        self.conv_output_size = model_config["variance_predictor"]["filter_size"]
        self.dropout = model_config["variance_predictor"]["dropout"]

        self.conv_layer = nn.Sequential(
            OrderedDict(
                [
                    (
                        "conv1d_1",
                        ConvNorm(
                            self.input_size,
                            self.filter_size,
                            kernel_size=self.kernel,
                            stride=1,
                            padding=(self.kernel - 1) // 2,
                            dilation=1,
                            channel_last=True,
                        ),
                    ),
                    ("relu_1", nn.ReLU()),
                    ("layer_norm_1", nn.LayerNorm(self.filter_size)),
                    ("dropout_1", nn.Dropout(self.dropout)),
                    (
                        "conv1d_2",
                        ConvNorm(
                            self.filter_size,
                            self.filter_size,
                            kernel_size=self.kernel,
                            stride=1,
                            padding=1,
                            dilation=1,
                            channel_last=True,
                        ),
                    ),
                    ("relu_2", nn.ReLU()),
                    ("layer_norm_2", nn.LayerNorm(self.filter_size)),
                    ("dropout_2", nn.Dropout(self.dropout)),
                ]
            )
        )

        self.linear_layer = nn.Linear(self.conv_output_size, 1)

    def forward(self, encoder_output, mask):
        out = self.conv_layer(encoder_output)
        out = self.linear_layer(out)
        out = out.squeeze(-1)

        if mask is not None:
            out = out.masked_fill(mask, 0.0)

        return out