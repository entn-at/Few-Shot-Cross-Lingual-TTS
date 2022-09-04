import torch
import torch.nn.functional as F
import numpy as np
import pytorch_lightning as pl
import random
from tqdm import tqdm
from contextlib import contextmanager
import matplotlib
from matplotlib import pyplot as plt
matplotlib.use("Agg")

from Parsers.parser import DataParser
from text import text_to_sequence
from text.define import LANG_ID2SYMBOLS
import Define


def numpy_exist_nan(x: np.array):
    return np.any(np.isnan(x))


def torch_exist_nan(x: torch.Tensor):
    return (x != x).any()


class LightningMelGAN(pl.LightningModule):
    def __init__(self):
        super().__init__()
        vocoder = torch.hub.load(
            "descriptinc/melgan-neurips", "load_melgan", "multi_speaker"
        )
        self.mel2wav = vocoder.mel2wav

    def inverse(self, mel):
        with torch.no_grad():
            return self.mel2wav(mel).squeeze(1)

    def infer(self, mels, max_wav_value, lengths=None):
        """preprocess_config["preprocessing"]["audio"]["max_wav_value"]
        """
        wavs = self.inverse(mels / np.log(10))
        wavs = (wavs.cpu().numpy() * max_wav_value).astype("int16")
        wavs = [wav for wav in wavs]

        for i in range(len(mels)):
            if lengths is not None:
                wavs[i] = wavs[i][: lengths[i]]
        return wavs


@contextmanager
def seed_all(seed=None, devices=None):
    rstate = random.getstate()
    nstate = np.random.get_state()
    with torch.random.fork_rng(devices):
        random.seed(seed)
        np.random.seed(seed)
        if seed is None:
            seed = torch.seed()
            torch.cuda.manual_seed_all(seed)
        else:
            torch.manual_seed(seed)
            torch.cuda.manual_seed_all(seed)
        yield
    random.setstate(rstate)
    np.random.set_state(nstate)


def get_mask_from_lengths(lengths, max_len=None):
    batch_size = lengths.shape[0]
    if max_len is None:
        max_len = torch.max(lengths).item()

    ids = torch.arange(0, max_len).unsqueeze(0).expand(batch_size, -1).to(Define.DEVICE)
    mask = ids >= lengths.unsqueeze(1).expand(-1, max_len)

    return mask


def expand(values, durations):
    out = list()
    for value, d in zip(values, durations):
        out += [value] * max(0, int(d))
    return np.array(out)


def plot_mel(data, stats, titles):
    fig, axes = plt.subplots(len(data), 1, squeeze=False)
    if titles is None:
        titles = [None for i in range(len(data))]
    pitch_min, pitch_max, pitch_mean, pitch_std, energy_min, energy_max = stats
    pitch_min = pitch_min * pitch_std + pitch_mean
    pitch_max = pitch_max * pitch_std + pitch_mean

    def add_axis(fig, old_ax):
        ax = fig.add_axes(old_ax.get_position(), anchor="W")
        ax.set_facecolor("None")
        return ax

    fig.subplots_adjust(hspace=0.3)
    for i in range(len(data)):
        mel, pitch, energy = data[i]
        pitch = pitch * pitch_std + pitch_mean
        axes[i][0].imshow(mel, origin="lower")
        axes[i][0].set_aspect(2.5, adjustable="box")
        axes[i][0].set_ylim(0, mel.shape[0])
        axes[i][0].set_title(titles[i], fontsize="medium")
        axes[i][0].tick_params(labelsize="x-small", left=False, labelleft=False)
        axes[i][0].set_anchor("W")

        ax1 = add_axis(fig, axes[i][0])
        ax1.plot(pitch, color="tomato")
        ax1.set_xlim(0, mel.shape[1])
        ax1.set_ylim(0, pitch_max)
        ax1.set_ylabel("F0", color="tomato")
        ax1.tick_params(
            labelsize="x-small", colors="tomato", bottom=False, labelbottom=False
        )

        ax2 = add_axis(fig, axes[i][0])
        ax2.plot(energy, color="darkviolet")
        ax2.set_xlim(0, mel.shape[1])
        ax2.set_ylim(energy_min, energy_max)
        ax2.set_ylabel("Energy", color="darkviolet")
        ax2.yaxis.set_label_position("right")
        ax2.tick_params(
            labelsize="x-small",
            colors="darkviolet",
            bottom=False,
            labelbottom=False,
            left=False,
            labelleft=False,
            right=True,
            labelright=True,
        )

    return fig


def pad_1D(inputs, PAD=0):
    def pad_data(x, length, PAD):
        x_padded = np.pad(
            x, (0, length - x.shape[0]), mode="constant", constant_values=PAD
        )
        return x_padded

    max_len = max((len(x) for x in inputs))
    padded = np.stack([pad_data(x, max_len, PAD) for x in inputs])

    return padded


def pad_2D(inputs, maxlen=None):
    def pad(x, max_len):
        PAD = 0
        if np.shape(x)[0] > max_len:
            raise ValueError("not max_len")

        s = np.shape(x)[1]
        x_padded = np.pad(
            x, (0, max_len - np.shape(x)[0]), mode="constant", constant_values=PAD
        )
        return x_padded[:, :s]

    if maxlen:
        output = np.stack([pad(x, maxlen) for x in inputs])
    else:
        max_len = max(np.shape(x)[0] for x in inputs)
        output = np.stack([pad(x, max_len) for x in inputs])

    return output


def pad(input_ele, mel_max_length=None):
    if mel_max_length:
        max_len = mel_max_length
    else:
        max_len = max([input_ele[i].size(0) for i in range(len(input_ele))])

    out_list = list()
    for i, batch in enumerate(input_ele):
        if len(batch.shape) == 1:
            one_batch_padded = F.pad(
                batch, (0, max_len - batch.size(0)), "constant", 0.0
            )
        elif len(batch.shape) == 2:
            one_batch_padded = F.pad(
                batch, (0, 0, 0, max_len - batch.size(0)), "constant", 0.0
            )
        out_list.append(one_batch_padded)
    out_padded = torch.stack(out_list)
    return out_padded


def read_queries_from_txt(path):
    res = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f.readlines():
            n, s, t, r = line.strip("\n").split("|")
            res.append({
                "basename": n,
                "spk": s,
            })
    return res


def generate_reference(path, data_parser: DataParser, lang_id):
    """
    Generate information similar to those from dataloader, which are inputs of refernece extractors.
    """
    info = {
        "lang_id": lang_id,
        "n_symbols": len(LANG_ID2SYMBOLS[lang_id]),
        "avg-frames": [],
        "raw-feat": [],
        "phonemes": [],
    }
    queries = read_queries_from_txt(path)[:64]
    for query in tqdm(queries):
        if Define.UPSTREAM == "mel":
            duration = data_parser.mfa_duration.read_from_query(query)
            info["avg-frames"].append(duration)
            mel = data_parser.mel.read_from_query(query)
            info["raw-feat"].append(torch.from_numpy(mel).float())
        else:
            ssl_wav = data_parser.wav_trim_16000.read_from_query(query)
            info["raw-feat"].append(torch.from_numpy(ssl_wav).float())
            mfa_segment = data_parser.mfa_segment.read_from_query(query)

            avg_frames_16000 = []
            for (s, e) in mfa_segment:
                avg_frames_16000.append(
                    int(
                        np.round(e * 50)  # All ssl model use 20ms window
                        - np.round(s * 50)
                    )
                )
            info["avg-frames"].append(avg_frames_16000)

        phns = data_parser.phoneme.read_from_query(query)
        phns = f"{{{phns}}}"  # match input format of text_to_sequence()
        phone = np.array(text_to_sequence(phns, ["basic_cleaners"], lang_id))
        info["phonemes"].append(phone)

    return info


# Origin Author: Daniel Lin
def ssl_match_length(inputs, target_len: int):
    """
    Since the upstream extraction process can sometimes cause a mismatch
    between the seq lenth of inputs and labels:
    - if len(inputs) > len(labels), we truncate the final few timestamp of inputs to match the length of labels
    - if len(inputs) < len(labels), we duplicate the last timestep of inputs to match the length of labels
    Note that the length of labels should never be changed.
    Input is always SSL feature with shape (B, L, *dim).
    """
    factors = [1] * inputs.dim()
    input_len, label_len = inputs.size(1), target_len
    if input_len > label_len:
        inputs = inputs[:, :label_len, :]
    elif input_len < label_len:
        pad_vec = inputs[:, -1, :].unsqueeze(1)  # (batch_size, 1, *dim)
        factors[1] = label_len - input_len
        inputs = torch.cat((inputs, pad_vec.repeat(*factors)), dim=1)  # (batch_size, seq_len, *dim), where seq_len == target_len
    return inputs
