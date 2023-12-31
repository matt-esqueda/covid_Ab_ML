""" Bidirectional LSTM model
"""
import os
import re
import sys
import torch
import numpy as np
import pandas as pd
import seaborn as sns
from datetime import date
from matplotlib import pyplot as plt
from torch import nn
from torch.nn.utils.rnn import pad_sequence
from torch.utils.data import Dataset
from torch.utils.data import DataLoader
from torch.utils.data import random_split
from prettytable import PrettyTable
from tape import ProteinBertModel, TAPETokenizer

class Transformer:
    """Transforms input sequence to features"""

    def __init__(self, method: str, **kwargs):
        self.tape_tokenizer = TAPETokenizer(vocab='iupac')
        if method == 'one_hot':
            self.method = 'one_hot'

        elif method == 'tape':
            self.method = 'tape'
            # prepare for TAPE
            self.tape_model = ProteinBertModel.from_pretrained('bert-base')
            self.tape_tokenizer = TAPETokenizer(vocab='iupac')

        else:
            print('unimplemented transform method', file=sys.stderr)
            sys.exit(1)

    def _load_glove_vect(self):
        """load Glove word vector file"""
        self.glove_kmer_dict = {}
        with open(self.glove_csv, 'r') as fh:
            for k_line in fh.readlines():
                k_line = k_line.rstrip().split()
                kmer = k_line[0]
                vects = k_line[1:]
                vects = torch.tensor([float(x) for x in vects], requires_grad=True)
                self.glove_kmer_dict.update({kmer: vects})
            self.glove_vec_size = len(vects)


    def embed(self, max_len, seq) -> torch.Tensor:
        """Embed sequence feature using a defind method.

        seq: sequence of input.
        """

        if self.method == 'one_hot':
            return self._embed_one_hot(max_len,seq)

        if self.method == 'tape':
            return self._embed_tape(max_len,seq)

        print(f'Undefined embedding method: {self.method}', file=sys.stderr)
        sys.exit(1)

    def _embed_tape(self, max_len: int, seq: str) -> torch.Tensor:
        """Embed sequence freature using TAPE.

        TAPE model gives two embeddings, one seq level embedding and one pooled_embedding.
        We will use the seq level emedding since the authors of TAPE suggests its peformance
        is superiror compared to the pooled embeddng.
        .
        """
        _token = self.tape_tokenizer.encode(seq)
        try:
            pad_len = max_len - len(seq)
            padded_token = np.pad(_token, (0, pad_len), 'constant')
            token = torch.tensor(np.array([padded_token]))
        except ValueError:
            print(len(_token))
            print(len(seq))
            print(_token)
        with torch.no_grad():
            seq_embedding, pooled_embedding = self.tape_model(token)
        
        return torch.squeeze(seq_embedding)

    def _embed_one_hot(self, max_len: int, seq: str) -> torch.Tensor:
        """Embed sequence feature using one-hot."""
        _token = self.tape_tokenizer.encode(seq)
        try:
            pad_len = max_len - len(seq)
            padded_token = np.pad(_token, (0, pad_len), 'constant')
            token = torch.tensor(np.array([padded_token]))
            embedding = nn.functional.one_hot(token)
        except KeyError:
            print(f'Unknown key', file=sys.stderr)
            sys.exit(1)
        return embedding


class AlphpaSeqDataset(Dataset):
    """Binding dataset."""
    def __init__(self, csv_file: str, transformer: Transformer):
        """
        Load sequence label and binding data from csv file and generate full
        sequence using the label and refseq.

        csv_file: a csv file with sequence and kinetic data.
        refseq: reference sequence (wild type sequence)
        transformer: a Transfomer object for embedding features.
        """

        def _load_csv():
            labels = []
            log10_ka = []
            seqs = []
            try:
                with open(csv_file, 'r') as fh:
                    next(fh) #skip header
                    for line in fh.readlines():
                        line = line.split(',')
                        labels.append(line[0])
                        seqs.append(line[1])
                        log10_ka.append(np.float32(line[14]))
            except FileNotFoundError:
                print(f'File not found error: {csv_file}.', file=sys.stderr)
                sys.exit(1)
            return labels, seqs, log10_ka

        self.csv_file = csv_file
        self.labels, self.seqs, self.log10_ka = _load_csv()
        self._longest_seq = len(max(self.seqs,key=len))
        self.transformer = transformer

    def __len__(self) -> int:
        return len(self.labels)
    
    def __getitem__(self, idx):
        try:
            features = self.transformer.embed(self._longest_seq,self.seqs[idx])
            return self.labels[idx], features, self.log10_ka[idx]
        except IndexError:
            print(f'List index out of range: {idx}, length: {len(self.labels)}.',
                  file=sys.stderr)
            sys.exit(1)

    
class BLSTM(nn.Module):
    """Bidirectional LSTM
    """
    def __init__(self,
                 batch_size,         # Batch size of the tensor
                 lstm_input_size,    # The number of expected features.
                 lstm_hidden_size,   # The number of features in hidden state h.
                 lstm_num_layers,    # Number of recurrent layers in LSTM.
                 lstm_bidirectional, # Bidrectional LSTM.
                 fcn_hidden_size,    # The number of features in hidden layer of CN.
                 device):            # Device ('cpu' or 'cuda')
        super().__init__()
        self.batch_size = batch_size
        self.device = device


        # LSTM layer
        self.lstm = nn.LSTM(input_size=lstm_input_size,
                            hidden_size=lstm_hidden_size,
                            num_layers=lstm_num_layers,
                            bidirectional=lstm_bidirectional,
                            batch_first=True)               # batch first

        # FCN fcn layer
        if lstm_bidirectional:
            self.fcn = nn.Linear(2 * lstm_hidden_size, fcn_hidden_size)
        else:
            self.fcn = nn.Linear(lstm_hidden_size, fcn_hidden_size)

        # FCN output layer
        self.out = nn.Linear(fcn_hidden_size, 1)

    def forward(self, x):
        # Initialize hidden and cell states to zeros.
        num_directions = 2 if self.lstm.bidirectional else 1
        h_0 = torch.zeros(num_directions * self.lstm.num_layers,
                          x.size(0),
                          self.lstm.hidden_size).to(self.device)
        c_0 = torch.zeros(num_directions * self.lstm.num_layers,
                          x.size(0),
                          self.lstm.hidden_size).to(self.device)

        # call lstm with input, hidden state, and internal state
        lstm_out, (h_n, c_n) = self.lstm(x, (h_0, c_0))
        h_n.detach()
        c_n.detach()
        lstm_final_out = lstm_out[:,-1,:]  # last hidden state from every batch. size: N*H_cell
        lstm_final_state = lstm_final_out.to(self.device)
        fcn_out = self.fcn(lstm_final_state)
        prediction = self.out(fcn_out)
        return prediction


def run_lstm(model: BLSTM,
             train_set: Dataset,
             test_set: Dataset,
             n_epochs: int,
             batch_size: int,
             device: str,
             save_as: str):
    """Run LSTM model

    model: BLSTM,
    train_set: training set dataset
    test_set: test det dataset
    n_epochs: number of epochs
    batch_size: batch size
    device: 'gpu' or 'cpu'
    save_as: path and file name to save the model results
    """

    L_RATE = 1e-5               # learning rate
    model = model.to(device)



    loss_fn = nn.MSELoss(reduction='sum').to(device)  # MSE loss with sum
    optimizer = torch.optim.SGD(model.parameters(), L_RATE)  # SGD optimizer

    train_loss_history = []
    test_loss_history = []
    for epoch in range(1, n_epochs + 1):
        train_loss = 0
        test_loss = 0

        train_loader = DataLoader(train_set, batch_size=BATCH_SIZE, shuffle=True)
        test_loader = DataLoader(test_set, batch_size=BATCH_SIZE, shuffle=True)


        for batch, (label, feature, target) in enumerate(train_loader):
            optimizer.zero_grad()
            feature, target = feature.to(device), target.to(device)
            pred = model(feature).flatten()
            batch_loss = loss_fn(pred, target)        # MSE loss at batch level
            train_loss += batch_loss.item()
            batch_loss.backward()
            optimizer.step()


        for batch, (label, feature, target) in enumerate(test_loader):
            feature, target = feature.to(device), target.to(device)
            with torch.no_grad():
                pred = model(feature).flatten()
                batch_loss = loss_fn(pred, target)
                test_loss += batch_loss.item()

        train_loss_history.append(train_loss)
        test_loss_history.append(test_loss)

        if epoch < 11:
            print(f'Epoch {epoch}, Train MSE: {train_loss}, Test MSE: {test_loss}')
        elif epoch%10 == 0:
            print(f'Epoch {epoch}, Train MSE: {train_loss}, Test MSE: {test_loss}')

        save_model(model, optimizer, epoch, save_as + '.model_save')

    return train_loss_history, test_loss_history


def save_model(model: BLSTM, optimizer: torch.optim.SGD, epoch: int, save_as: str):
    """Save model parameters.

    model: a BLSTM model object
    optimizer: model optimizer
    epoch: number of epochs in the end of the model running
    save_as: file name for saveing the model.
    """
    torch.save({'epoch': epoch,
                'model_state_dict': model.state_dict(),
                'optimizer_state_dict': optimizer.state_dict()},
               save_as)

def plot_history(train_losses: list, n_train: int, test_losses: list,
                 n_test: int, save_as: str):
    """Plot training and testing history per epoch

    train_losses: a list of per epoch error from the training set
    n_train: number of items in the training set
    test_losses: a list of per epoch error from the test set
    n_test: number of items in the test set
    """
    history_df = pd.DataFrame(list(zip(train_losses, test_losses)),
                              columns = ['training','testing'])

    history_df['training'] = history_df['training']/n_train  # average error per item
    history_df['testing'] = history_df['testing']/n_test

    print(history_df)

    sns.set_theme()
    sns.set_context('talk')

    plt.ion()
    fig = plt.figure(figsize=(8, 6))
    ax = sns.scatterplot(data=history_df, x=history_df.index, y='training', label='training')
    sns.scatterplot(data=history_df, x=history_df.index, y='testing', label='testing')
    ax.set(xlabel='Epochs', ylabel='Average MSE per sample', title='Effect of Epochs on MSE')       # added title to graph

    fig.savefig(save_as + '.png')
    history_df.to_csv(save_as + '.csv')

def count_parameters(model):
    """Count model parameters and print a summary

    A nice hack from:
    https://stackoverflow.com/a/62508086/1992369
    """
    table = PrettyTable(["Modules", "Parameters"])
    total_params = 0
    for name, parameter in model.named_parameters():
        if not parameter.requires_grad: continue
        params = parameter.numel()
        table.add_row([name, params])
        total_params+=params
    print(table)
    print(f"Total Trainable Params: {total_params}\n")
    return total_params

if __name__=='__main__':
    ROOT_DIR = os.path.join(os.path.dirname(__file__), '..')
    ROOT_DIR = os.path.abspath(ROOT_DIR)
    DATA_DIR = os.path.join(ROOT_DIR, 'regression_model')


    KD_CSV = os.path.join(DATA_DIR, 'test_reg.csv')
    
    # Run setup
    DEVICE = 'cuda' if torch.cuda.is_available else 'cpu'
    BATCH_SIZE = 32
    N_EPOCHS = 20

    ## TAPE LSTMb
    LSTM_INPUT_SIZE = 768       # lstm_input_size
    LSTM_HIDDEN_SIZE = 50       # lstm_hidden_size
    LSTM_NUM_LAYERS = 1         # lstm_num_layers
    LSTM_BIDIRECTIONAL = True   # lstm_bidrectional
    FCN_HIDDEN_SIZE = 100        # fcn_hidden_size
    
    # tape_transformer = Transformer('one_hot')
    tape_transformer = Transformer('tape')
    data_set = AlphpaSeqDataset(KD_CSV, tape_transformer)
    print(data_set[0])
    
    TRAIN_SIZE = int(0.8 * len(data_set))  # 80% goes to training.
    TEST_SIZE = len(data_set) - TRAIN_SIZE
    train_set, test_set = random_split(data_set, (TRAIN_SIZE, TEST_SIZE))

    model = BLSTM(BATCH_SIZE,
                  LSTM_INPUT_SIZE,
                  LSTM_HIDDEN_SIZE,
                  LSTM_NUM_LAYERS,
                  LSTM_BIDIRECTIONAL,
                  FCN_HIDDEN_SIZE,
                  DEVICE)

    count_parameters(model)
    model_result = f'blstm_TAPE_epochs_{N_EPOCHS}_train_{TRAIN_SIZE}_test_{TEST_SIZE}_{date.today()}'        # added N_EPOCHS to file name 
    model_result = os.path.join(DATA_DIR, f'plots/{model_result}')      # changed path to DATA_DIR
    train_losses, test_losses = run_lstm(model, train_set, test_set,
                                         N_EPOCHS, BATCH_SIZE, DEVICE, model_result)
    plot_history(train_losses, TRAIN_SIZE, test_losses, TEST_SIZE, model_result)
    
    
    

    
