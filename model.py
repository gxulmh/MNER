import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence


class MNER(torch.nn.Module):
    def __init__(self, params, pretrained_weight=None, num_of_tags=10):
        super(MNER, self).__init__()
        self.params = params
        self.text_encoder = Encoder(params, pretrained_weight)
        self.linear_transform = nn.Linear(in_features=params.visual_feature_dimension,
                                          out_features=2 * params.hidden_dimension)
        # Get visually attended features
        self.sca = StackedCrossAttention(params.lambda_1)
        self.gate = FilterGate(params)
        self.dropout = nn.Dropout(params.dropout)
        self.projection = nn.Linear(in_features=2 * params.hidden_dimension if self.params.use_filter_gate == 1
                                      else 4 * params.hidden_dimension, out_features=num_of_tags)
        self.lsm = nn.LogSoftmax(dim=2)

    def forward(self, sentence, image, sentence_lens, mask, chars):
        # Get the text features
        u = self.text_encoder(sentence, sentence_lens, chars)

        # Get the image features
        v = self.linear_transform(image)

        similarity_matrix = torch.bmm(F.normalize(input=v, p=2, dim=2),
                                      F.normalize(input=u,
                                                  p=2, dim=2).permute(0, 2, 1))                 # bs * roi * max_seq_len
        s_t = F.normalize(input=similarity_matrix.clamp(min=0), p=2, dim=2)

        # Attended image vector
        a_v = self.sca(u, mask, v, s_t)

        # Apply filteration gate
        if self.params.use_filter_gate == 1:
            if self.params.use_only_text == 1:
                m = u
            else:
                m = self.gate(u, a_v)                                                             # bs * seq_len * (2 * hidden)
        else:
            m = torch.cat((u, a_v), dim=2)

        # projecting to labels
        out = self.projection(m)
        out = self.lsm(out)
        return out.permute(1, 0, 2)                                                       # seq_len * bs * tags


class Encoder(torch.nn.Module):
    def __init__(self, params, pretrained_weight):
        super(Encoder, self).__init__()
        self.word_embeddings = nn.Embedding(params.vocab_size, params.embedding_dimension)
        self.params = params
        self.dropout = nn.Dropout(params.dropout)
        if self.params.use_char_embedding == 1:
            self.char_embeddings = nn.Embedding(params.char_vocab_size, params.embedding_dimension_char)
            self.conv_bi = nn.Conv1d(in_channels=params.embedding_dimension_char, out_channels=params.hidden_dimension_char,
                                     kernel_size=2, padding=1)
            self.conv_tri = nn.Conv1d(in_channels=params.hidden_dimension_char, out_channels=params.hidden_dimension_char,
                                      kernel_size=3, padding=2)
            self.conv_quad = nn.Conv1d(in_channels=params.hidden_dimension_char, out_channels=params.hidden_dimension_char,
                                       kernel_size=4, padding=3)
            self.fc_conv = nn.Linear(in_features=3 * params.hidden_dimension_char, out_features=params.hidden_dimension_char)

        if pretrained_weight is not None:
            # Assigning pre-trained embeddings as initial weights
            self.word_embeddings.weight.data.copy_(torch.from_numpy(pretrained_weight))

        self.lstm = nn.LSTM(input_size=params.embedding_dimension + params.hidden_dimension_char
                            if params.use_char_embedding == 1 else params.embedding_dimension,
                            hidden_size=params.hidden_dimension,
                            num_layers=params.n_layers, bidirectional=True)

    def forward(self, seq, seq_lens, chars):
        seq = seq.transpose(0, 1)  # seq_len * batch_size * embedding_dimension
        embeds = self.word_embeddings(seq)

        if self.params.use_char_embedding == 1:
            # TODO: Check this char embedding later
            char_embedddings = self.char_embeddings(chars.view(-1, chars.size(2))).view(*chars.size(), -1)
            char_size = char_embedddings.size()
            # first transform to [batch *length, char_length, char_dim]
            # then transpose to [batch * length, char_dim, char_length]
            char = char_embedddings.view(char_size[0] * char_size[1], char_size[2], char_size[3]).transpose(1, 2)
            # put into cnn [batch*length, char_filters, char_length]
            # then put into maxpooling [batch * length, char_filters]
            char, _ = self.conv_bi(char).max(dim=2)
            # reshape to [batch, length, char_filters]
            char = F.relu(char).view(char_size[0], char_size[1], -1)
            # char = char.permute(0, 2, 1)
            # char, _ = self.conv_tri(char).max(dim=2)
            # char = F.relu(char).view(char_size[0], char_size[1], -1)
            # char, _ = self.conv_quad(char).max(dim=2)
            # char = F.relu(char).view(char_size[0], char_size[1], -1)

            char = char.permute(1, 0, 2)
            embeds = torch.cat((embeds, char), dim=2)

        embeds = self.dropout(embeds)
        packed_input = pack_padded_sequence(embeds, seq_lens.numpy())
        packed_outputs, _ = self.lstm(packed_input)
        outputs, _ = pad_packed_sequence(packed_outputs)
        outputs = self.dropout(outputs)
        return outputs.permute(1, 0, 2)  # batch_size * seq_len * hidden_dimension


class StackedCrossAttention(torch.nn.Module):
    def __init__(self, lambda_1):
        super(StackedCrossAttention, self).__init__()
        self.lambda_1 = lambda_1

    def forward(self, h, mask, img, s_t):
        alpha_t = self.lambda_1 * s_t
        alpha_t.data.masked_fill_((1 - mask).data.unsqueeze(1).byte(), -float('inf'))
        alpha = F.softmax(input=alpha_t, dim=1)                                          # bs * roi * max_seq_len
        alpha.data.masked_fill_((1 - mask).data.unsqueeze(1).byte(), 0)
        attended_image_vectors = torch.bmm(alpha.permute(0, 2, 1), img)                  # bs * max_seq_len * hidden_dim
        return attended_image_vectors


class FilterGate(torch.nn.Module):
    def __init__(self, params):
        super(FilterGate, self).__init__()
        self.w_s_h = nn.Linear(in_features=2 * params.hidden_dimension, out_features=params.hidden_dimension, bias=False)
        self.w_m_s = nn.Linear(in_features=2 * params.hidden_dimension, out_features=params.hidden_dimension)
        self.w_m = nn.Linear(in_features=2 * params.hidden_dimension, out_features=2 * params.hidden_dimension)
        self.w_m_ = nn.Linear(in_features=4 * params.hidden_dimension, out_features=2 * params.hidden_dimension, bias=False)

    def forward(self, h, m):
        gate_val = F.sigmoid(torch.cat((self.w_s_h(h), self.w_m_s(m)), dim=2))
        u = gate_val * F.tanh(self.w_m(m))
        m_ = self.w_m_(torch.cat((h, u), dim=2))
        return m_
