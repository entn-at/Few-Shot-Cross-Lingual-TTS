import torch
import pytorch_lightning as pl
from torch.utils.data import DataLoader, ConcatDataset

import Define
from lightning.collates import LanguageCollate, TextCollate
from lightning.datasets.language import FastSpeech2Dataset, TextDataset, SSLUnitPseudoLabelDataset, SSLUnitFSCLDataset, NoisyFastSpeech2Dataset
from ..utils import EpisodicInfiniteWrapper


class FastSpeech2DataModule(pl.LightningDataModule):
    """
    Train: FastSpeech2Dataset + LanguageCollate.
    Val: FastSpeech2Dataset + LanguageCollate.
    Test: TextDataset.
    """
    def __init__(self, data_configs, train_config, algorithm_config, log_dir, result_dir, dataset_cls=FastSpeech2Dataset):
        super().__init__()
        self.data_configs = data_configs
        self.train_config = train_config
        self.algorithm_config = algorithm_config

        self.log_dir = log_dir
        self.result_dir = result_dir
        self.val_step = self.train_config["step"]["val_step"]

        self.re_id = True  # TODO: should be controlled by client directly
        self.collate = LanguageCollate()
        self.collate2 = TextCollate()

        self.dataset_cls = dataset_cls

    def setup(self, stage=None):
        spk_refer_wav = (self.algorithm_config["adapt"]["speaker_emb"]
                     in ["dvec", "encoder", "scratch_encoder"])

        if stage in (None, 'fit', 'validate'):
            self.train_datasets = [
                # self.dataset_cls(
                NoisyFastSpeech2Dataset(
                # SSLUnitPseudoLabelDataset(
                # SSLUnitFSCLDataset(
                    data_config['subsets']['train'],
                    Define.DATAPARSERS[data_config["name"]],
                    data_config, spk_refer_wav=spk_refer_wav
                ) for data_config in self.data_configs if 'train' in data_config['subsets']
            ]
            self.val_datasets = [
                FastSpeech2Dataset(
                    data_config['subsets']['val'],
                    Define.DATAPARSERS[data_config["name"]],
                    data_config, spk_refer_wav=spk_refer_wav
                ) for data_config in self.data_configs if 'val' in data_config['subsets']
            ]
            self.train_dataset = ConcatDataset(self.train_datasets)
            self.val_dataset = ConcatDataset(self.val_datasets)
            self._train_setup()
            self._validation_setup()

        if stage in (None, 'test', 'predict'):
            self.test_datasets = [
                TextDataset(
                    data_config['subsets']['test'],
                    Define.DATAPARSERS[data_config["name"]],
                    data_config
                ) for data_config in self.data_configs if 'test' in data_config['subsets']
            ]
            self.test_dataset = ConcatDataset(self.test_datasets)
            self._test_setup()

    def _train_setup(self):
        if not isinstance(self.train_dataset, EpisodicInfiniteWrapper):
            # self.batch_size = self.train_ways * (self.train_shots + self.train_queries) * self.meta_batch_size
            self.batch_size = self.train_config["optimizer"]["batch_size"]
            self.train_dataset = EpisodicInfiniteWrapper(self.train_dataset, self.val_step*self.batch_size)

    def _validation_setup(self):
        pass

    def _test_setup(self):
        pass

    def train_dataloader(self):
        """Training dataloader, not modified for multiple dataloaders."""
        self.train_loader = DataLoader(
            self.train_dataset,
            batch_size=self.batch_size//torch.cuda.device_count(),
            shuffle=True,
            drop_last=True,
            num_workers=Define.MAX_WORKERS,
            collate_fn=self.collate.collate_fn(False, re_id=self.re_id),  # CAUTION: tune does not need re_id
        )
        return self.train_loader

    def val_dataloader(self):
        """Validation dataloader, not modified for multiple dataloaders."""
        self.val_loader = DataLoader(
            self.val_dataset,
            batch_size=self.batch_size//torch.cuda.device_count(),
            shuffle=False,
            drop_last=False,
            num_workers=0,
            collate_fn=self.collate.collate_fn(False, re_id=self.re_id),
        )
        return self.val_loader

    def test_dataloader(self):
        """Test dataloader"""
        self.test_loader = DataLoader(
            self.test_dataset,
            batch_size=self.batch_size//torch.cuda.device_count(),
            shuffle=False,
            collate_fn=self.collate2.collate_fn(False, re_id=self.re_id),
        )
        return self.test_loader


class FastSpeech2TuneDataModule(FastSpeech2DataModule):
    def __init__(self, data_configs, train_config, algorithm_config, log_dir, result_dir):
        # super().__init__(data_configs, train_config, algorithm_config, log_dir, result_dir, dataset_cls=FastSpeech2Dataset)

        # SSL Units but mapped to phonemes (including identity map)
        # super().__init__(data_configs, train_config, algorithm_config, log_dir, result_dir, dataset_cls=SSLUnitPseudoLabelDataset)
        super().__init__(data_configs, train_config, algorithm_config, log_dir, result_dir, dataset_cls=SSLUnitFSCLDataset)

        self.re_id = False
