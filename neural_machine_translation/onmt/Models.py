import torch
import torch.nn as nn
from torch.autograd import Variable
import torch.nn.functional as F
import os
import onmt
import onmt.modules
from onmt.modules import aeq
from onmt.modules.Gate import ContextGateFactory
from torch.nn.utils.rnn import pad_packed_sequence as unpack
from torch.nn.utils.rnn import pack_padded_sequence as pack
import math
import numpy as np
import pdb
import evaluation

class Embeddings(nn.Module):
    def __init__(self, opt, dicts, feature_dicts=None):
        super(Embeddings, self).__init__()

        self.positional_encoding = opt.position_encoding
        if self.positional_encoding:
            self.pe = self.make_positional_encodings(opt.word_vec_size, 5000).cuda()

        self.word_vec_size = opt.word_vec_size
        self.word_lut = nn.Embedding(dicts.size(), opt.word_vec_size, padding_idx=onmt.Constants.PAD)  # Word embeddings.
        self.dropout = nn.Dropout(p=opt.dropout)
        self.feature_dicts = feature_dicts
        self.feature_luts = nn.ModuleList([])

    def make_positional_encodings(self, dim, max_len):
        pe = torch.FloatTensor(max_len, 1, dim).fill_(0)
        for i in range(dim):
            for j in range(max_len):
                k = float(j) / (10000.0 ** (2.0 * i / float(dim)))
                pe[j, 0, i] = math.cos(k) if i % 2 == 1 else math.sin(k)
        return pe

    def load_pretrained_vectors(self, emb_file):
        if emb_file is not None:
            pretrained = torch.load(emb_file)
            self.word_lut.weight.data.copy_(pretrained)

    def forward(self, src_input):
        """
        Embed the words or utilize features and MLP.
        Args: src_input (LongTensor): len x batch x nfeat
        Return: emb (FloatTensor): len x batch x input_size
        """
        word = self.word_lut(src_input[:, :, 0])
        emb = word
        if self.positional_encoding:
            emb = emb + Variable(self.pe[:emb.size(0), :1, :emb.size(2)].expand_as(emb))
            emb = self.dropout(emb)
        return emb


class Encoder(nn.Module):
    """
    Encoder recurrent neural network.
    """
    def __init__(self, opt, dicts, feature_dicts=None):
        """
        Args:
            opt: Model options.
            dicts (`Dict`): The src dictionary
            features_dicts (`[Dict]`): List of src feature dictionaries.
        """
        super(Encoder, self).__init__()

        self.layers = opt.layers # Number of rnn layers.
        self.num_directions = 2 if opt.brnn else 1 # Use a bidirectional model.
        assert opt.rnn_size % self.num_directions == 0
        self.hidden_size = opt.rnn_size // self.num_directions  # Size of the encoder RNN.
        input_size = opt.word_vec_size
        self.embeddings = Embeddings(opt, dicts, feature_dicts)
        self.encoder_layer = opt.encoder_layer # The Encoder RNN.

        self.rnn = getattr(nn, opt.rnn_type)(input_size, self.hidden_size, num_layers=opt.layers, dropout=opt.dropout, bidirectional=opt.brnn)

        self.fertility = opt.fertility
        self.predict_fertility = opt.predict_fertility
        self.supervised_fertility = opt.supervised_fertility
        self.use_sigmoid_fertility = False  # True
        self.guided_fertility = opt.guided_fertility

    def forward(self, input, lengths=None, hidden=None):
        """
        Args:
            input (LongTensor): len x batch x nfeat
            lengths (LongTensor): batch
            hidden: Initial hidden state.

        Returns:
            hidden_t (FloatTensor): Pair of layers x batch x rnn_size - final
                                    Encoder state
            outputs (FloatTensor):  len x batch x rnn_size -  Memory bank
        """
        # CHECKS
        s_len, n_batch, n_feats = input.size()
        if lengths is not None:
            _, n_batch_ = lengths.size()
            aeq(n_batch, n_batch_)
        # END CHECKS

        emb = self.embeddings(input)
        s_len, n_batch, vec_size = emb.size()

        # Standard RNN encoder.
        packed_emb = emb
        if lengths is not None:
            # Lengths data is wrapped inside a Variable.
            lengths = lengths.data.view(-1).tolist()
            packed_emb = pack(emb, lengths)
        outputs, hidden_t = self.rnn(packed_emb, hidden)
        if lengths:
            outputs = unpack(outputs)[0]
        fertility_vals = None
        return hidden_t, outputs, fertility_vals

class Decoder(nn.Module):
    """
    Decoder + Attention recurrent neural network.
    """

    def __init__(self, opt, dicts):
        """
        Args:
            opt: model options
            dicts: Target `Dict` object
        """
        super(Decoder, self).__init__()

        self.layers = opt.layers
        self.decoder_layer = opt.decoder_layer
        self._coverage = opt.coverage_attn
        self.exhaustion_loss = opt.exhaustion_loss
        self.fertility_loss = False
        self.hidden_size = opt.rnn_size
        self.input_feed = opt.input_feed
        input_size = opt.word_vec_size
        if self.input_feed:
            input_size += opt.rnn_size
        self.embeddings = Embeddings(opt, dicts, None)
        if opt.rnn_type == "LSTM":
            stackedCell = onmt.modules.StackedLSTM
        else:
            stackedCell = onmt.modules.StackedGRU
        self.rnn = stackedCell(opt.layers, input_size, opt.rnn_size, opt.dropout)
        self.context_gate = None
        if opt.context_gate is not None:
            self.context_gate = ContextGateFactory(opt.context_gate, opt.word_vec_size, input_size, opt.rnn_size, opt.rnn_size)

        self.dropout = nn.Dropout(opt.dropout)
        # Std attention layer.
        self.attn = onmt.modules.GlobalAttention(opt.rnn_size, coverage=self._coverage, attn_type=opt.attention_type, attn_transform=opt.attn_transform, c_attn=opt.c_attn)
        self.fertility = opt.fertility
        self.predict_fertility = opt.predict_fertility
        self.guided_fertility = opt.guided_fertility
        self.supervised_fertility = opt.supervised_fertility
        # Separate Copy Attention.
        self._copy = False
        if opt.copy_attn:
            self.copy_attn = onmt.modules.GlobalAttention(opt.rnn_size, attn_type=opt.attention_type)
            self._copy = True

    def forward(self, input, src, context, state, fertility_vals=None, fert_dict=None, fert_sents=None,
                upper_bounds=None, test=False):
        """
        Forward through the decoder.

        Args:
            input (LongTensor):  (len x batch) -- Input tokens
            src (LongTensor)
            context:  (src_len x batch x rnn_size)  -- Memory bank
            state: an object initializing the decoder.

        Returns:
            outputs: (len x batch x rnn_size)
            final_states: an object of the same form as above
            attns: Dictionary of (src_len x batch)
        """
        # CHECKS
        t_len, n_batch = input.size()
        s_len, n_batch_, _ = src.size()
        s_len_, n_batch__, _ = context.size()
        aeq(n_batch, n_batch_, n_batch__)

        # aeq(s_len, s_len_)
        # END CHECKS
        emb = self.embeddings(input.unsqueeze(2))
        # n.b. you can increase performance if you compute W_ih * x for all iterations in parallel, but that's only possible if self.input_feed=False
        outputs = []

        # Setup the different types of attention.
        attns = {"std": []}
        if self._copy:
            attns["copy"] = []
        if self._coverage:
            attns["coverage"] = []
        if self.exhaustion_loss:
            attns["upper_bounds"] = []

        assert isinstance(state, RNNDecoderState)
        output = state.input_feed.squeeze(0)
        hidden = state.hidden
        # CHECKS
        n_batch_, _ = output.size()
        aeq(n_batch, n_batch_)
        # END CHECKS

        coverage = state.coverage.squeeze(0) if state.coverage is not None else None

        for i, emb_t in enumerate(emb.split(1)):
            # Initialize upper bounds for the current batch
            if upper_bounds is None:
                upper_bounds = Variable(torch.Tensor([self.fertility]).repeat(n_batch_, s_len_)).cuda()

            # Use <SINK> token for absorbing remaining attention weight
            upper_bounds[:, -1] = Variable(100. * torch.ones(upper_bounds.size(0)))

            emb_t = emb_t.squeeze(0)
            if self.input_feed:
                emb_t = torch.cat([emb_t, output], 1)

            rnn_output, hidden = self.rnn(emb_t, hidden)
            attn_output, attn = self.attn(rnn_output, context.transpose(0, 1), upper_bounds=upper_bounds)

            upper_bounds -= attn
            if self.context_gate is not None:
                output = self.context_gate(emb_t, rnn_output, attn_output)
                output = self.dropout(output)
            else:
                output = self.dropout(attn_output)
            outputs += [output]
            attns["std"] += [attn]

            # COVERAGE
            if self._coverage:
                coverage = (coverage + attn) if coverage else attn
                attns["coverage"] += [coverage]

            # COPY
            if self._copy:
                _, copy_attn = self.copy_attn(output, context.transpose(0, 1))
                attns["copy"] += [copy_attn]
            if self.exhaustion_loss:
                attns["upper_bounds"] += [upper_bounds]

        state = RNNDecoderState(hidden, output.unsqueeze(0),
                                coverage.unsqueeze(0)
                                if coverage is not None else None,
                                upper_bounds)
        outputs = torch.stack(outputs)
        for k in attns:
            attns[k] = torch.stack(attns[k])
        return outputs, state, attns, upper_bounds


class NMTModel(nn.Module):
    def __init__(self, encoder, decoder, multigpu=False):
        self.multigpu = multigpu
        super(NMTModel, self).__init__()
        self.encoder = encoder
        self.decoder = decoder

    def _fix_enc_hidden(self, h):
        """
        The encoder hidden is  (layers*directions) x batch x dim
        We need to convert it to layers x batch x (directions*dim)
        """
        if self.encoder.num_directions == 2:
            h = torch.cat([h[0:h.size(0):2], h[1:h.size(0):2]], 2)
        return h

    def init_decoder_state(self, context, enc_hidden):
        if isinstance(enc_hidden, tuple):
            dec = RNNDecoderState(tuple([self._fix_enc_hidden(enc_hidden[i]) for i in range(len(enc_hidden))]))
        else:
            dec = RNNDecoderState(self._fix_enc_hidden(enc_hidden))
        dec.init_input_feed(context, self.decoder.hidden_size)
        return dec

    def forward(self, src, tgt, lengths, dec_state=None, fert_dict=None, fert_sents=None):
        """
        Args:
            src, tgt, lengths
            dec_state: A decoder state object

        Returns:
            outputs (FloatTensor): (len x batch x rnn_size) -- Decoder outputs.
            attns (FloatTensor): Dictionary of (src_len x batch)
            dec_hidden (FloatTensor): tuple (1 x batch x rnn_size)
                                      Init hidden state
        """
        src = src
        tgt = tgt[:-1]  # exclude last target from inputs
        # print("src:", src)
        enc_hidden, context, fertility_vals = self.encoder(src, lengths)
        enc_state = self.init_decoder_state(context, enc_hidden)
        out, dec_state, attns, upper_bounds = self.decoder(tgt, src, context,
                                                           enc_state if dec_state is None
                                                           else dec_state, fertility_vals,
                                                           fert_dict, fert_sents)
        if self.multigpu:
            # Not yet supported on multi-gpu
            dec_state = None
            attns = None
        return out, attns, dec_state, upper_bounds

class DecoderState(object):
    def detach(self):
        for h in self.all:
            if h is not None:
                h.detach_()

    def repeatBeam_(self, beamSize):
        self._resetAll([Variable(e.data.repeat(1, beamSize, 1))
                        for e in self.all])

    def beamUpdate_(self, idx, positions, beamSize):
        for e in self.all:
            a, br, d = e.size()
            sentStates = e.view(a, beamSize, br // beamSize, d)[:, :, idx]
            sentStates.data.copy_(sentStates.data.index_select(1, positions))


class RNNDecoderState(DecoderState):
    def __init__(self, rnnstate, input_feed=None, coverage=None,
                 attn_upper_bounds=None):
        # all objects are X x batch x dim
        # or X x (beam * sent) for beam search
        if not isinstance(rnnstate, tuple):
            self.hidden = (rnnstate,)
        else:
            self.hidden = rnnstate
        self.input_feed = input_feed
        self.coverage = coverage
        self.attn_upper_bounds = attn_upper_bounds
        self.all = self.hidden + (self.input_feed,)

    def init_input_feed(self, context, rnn_size):
        batch_size = context.size(1)
        h_size = (batch_size, rnn_size)
        self.input_feed = Variable(context.data.new(*h_size).zero_(), requires_grad=False).unsqueeze(0)
        self.all = self.hidden + (self.input_feed,)

    def _resetAll(self, all):
        vars = [Variable(a.data if isinstance(a, Variable) else a, volatile=True) for a in all]
        self.hidden = tuple(vars[:-1])
        self.input_feed = vars[-1]
        self.all = self.hidden + (self.input_feed,)

    def beamUpdate_(self, idx, positions, beamSize):
        # I'm overriding this method to handle the upper bounds in the beam
        # updates. May be simpler to add this as part of self.all and not
        # do the overriding.
        # import pdb; pdb.set_trace()
        DecoderState.beamUpdate_(self, idx, positions, beamSize)
        if self.attn_upper_bounds is not None:
            e = self.attn_upper_bounds
            br, d = e.size()
            sentStates = e.view(beamSize, br // beamSize, d)[:, idx]
            sentStates.data.copy_(
                sentStates.data.index_select(0, positions))

class TransformerDecoderState(DecoderState):
    def __init__(self, input=None):
        # all objects are X x batch x dim
        # or X x (beam * sent) for beam search
        self.previous_input = input
        self.all = (self.previous_input,)

    def _resetAll(self, all):
        vars = [(Variable(a.data if isinstance(a, Variable) else a, volatile=True)) for a in all]
        self.previous_input = vars[0]
        self.all = (self.previous_input,)

    def repeatBeam_(self, beamSize):
        pass
