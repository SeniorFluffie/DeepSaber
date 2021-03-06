import logging
import math
import multiprocessing
import os
from typing import Tuple, Optional, Union

import gensim
import numpy as np
import pandas as pd

from process.compute import create_ogg_paths, generate_snippets, \
    add_previous_prediction  # split needed for gColab upload
from process.compute import process_song_folder, create_ogg_caches, remove_ogg_cache
from utils.functions import create_word_mapping, check_consistency
from utils.types import Config, Timer


def create_song_list(path):
    songs = []
    indicators = {'info.dat', 'info.json'}
    for root, _, files in os.walk(path, topdown=False):
        if bool(indicators.intersection(set([name.lower() for name in files]))):
            songs.append(root)

    return songs


def recalculate_mfcc_df_cache(song_folders, config: Config):
    """
    MFCC computation is memory heavy.
    Therefore recalculation catches SIGTERM through `multiprocessing`
    """
    if config.audio_processing.use_cache:
        return

    ogg_paths = create_ogg_paths(song_folders)
    remove_ogg_cache(ogg_paths)
    create_ogg_caches(ogg_paths, config)


def songs2dataset(song_folders, config: Config) -> Optional[pd.DataFrame]:
    print(f'\tCreate dataframe from songs in folders: {len(song_folders):7} folders')
    timer = Timer()
    recalculate_mfcc_df_cache(song_folders, config)
    timer('Recalculated MFCC cache')

    folders_to_process = len(song_folders)

    inputs = ((s, config, (i, folders_to_process)) for i, s in enumerate(song_folders))

    if config.use_multiprocessing:
        # `spawn` to sidestep POSIX fork pain: https://pythonspeed.com/articles/python-multiprocessing/
        with multiprocessing.get_context("spawn").Pool(10) as pool:
            songs = pool.starmap(process_song_folder, inputs)
        pool.close()
        pool.join()
        timer('Pool closed')
    else:
        songs = [process_song_folder(*x) for x in inputs]  # single core version for debugging
    timer('Computed partial dataframes from folders')

    songs = [x for x in songs if x is not None]
    timer('Filtered failed songs')

    if len(songs) == 0:
        logging.warning(f'Dataset creation collected 0 songs. Check if searching in correct folders.')
        return None
    df = pd.concat(songs)
    timer('Concatenated songs')

    df = df_post_processing(df, config)

    df = df.groupby(['name', 'difficulty']).apply(lambda x: generate_snippets(x, config=config))
    timer('Snippets generated')
    return df


def df_post_processing(df, config):
    if config.dataset.action_word_model_path.exists():
        action_model = gensim.models.KeyedVectors.load(str(config.dataset.action_word_model_path))

        df['word_vec'] = np.vsplit(action_model[df['word'].values].astype('float16'), len(df))
        df['word_vec'] = df['word_vec'].map(lambda x: x[0])

        word_id_dict = create_word_mapping(action_model)
        df['word_id'] = df['word'].map(lambda word: word_id_dict.get(word, 1))  # 0: MASK, 1: UNK
    else:
        logging.warning(f'Could not find action word model [{config.dataset.action_word_model_path}], '
                        f'skipping word_vec and word_id.')
        df['word_vec'] = 0
        df['word_id'] = 0

    df = df.groupby(['name', 'difficulty']).apply(lambda x: add_previous_prediction(x, config=config))

    return df


def infinite2zero(x: Union[np.array, float, int]):
    if type(x).__module__ == np.__name__:
        x[~np.isfinite(x)] = 0.0
        return x
    if not math.isfinite(x):
        return 0.0
    return x


def generate_datasets(song_folders, config: Config):
    timer = Timer()
    for phase, split in zip(['train', 'val', 'test'],
                            zip(config.training.data_split,
                                config.training.data_split[1:])
                            ):
        print('\n', '=' * 100, sep='')
        print(f'Processing {phase}')
        total = len(song_folders)
        split_from = int(total * split[0])
        split_to = int(total * split[1])
        result_path = config.dataset.storage_folder / f'{phase}_beatmaps.pkl'

        df = songs2dataset(song_folders[split_from:split_to], config=config)
        if df is None:
            logging.warning(f'Skipped {phase} dataset. No songs.')
            continue
        timer(f'Created {phase} dataset', 1)
        check_consistency(df)

        if phase == 'train':
            save_normalization_stats(df, config)
            timer(f'Saved normalization stats', 1)

        df = normalize_columns(df, config)
        timer(f'Normalized {phase} dataset', 1)

        config.dataset.storage_folder.mkdir(parents=True, exist_ok=True)
        df.to_pickle(result_path, protocol=4)  # Protocol 4 for Python 3.6/3.7 compatibility
        timer(f'Pickled {phase} dataset', 1)


def save_normalization_stats(df: pd.DataFrame, config: Config):
    regression_cols = config.dataset.cols_to_normalize
    for col in regression_cols:
        df[col] = df[col].apply(infinite2zero)
    mean = {col: np.stack(df[col].values).mean(0, dtype=np.float32) for col in regression_cols}
    std = {col: np.stack(df[col].values).std(0, dtype=np.float32) for col in regression_cols}
    pd.DataFrame({'mean': mean, 'std': std}).to_pickle(config.dataset.normalization_stats_path)
    return regression_cols


def normalize_columns(df: pd.DataFrame, config: Config):
    stats = pd.read_pickle(config.dataset.normalization_stats_path)

    for col in set(config.dataset.cols_to_normalize).intersection(df.columns):
        df[col] = df[col].apply(lambda x: (x - stats['mean'][col]) / (stats['std'][col] + 1e-6))

    return df


def load_datasets(config: Config) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    try:
        datasets = [pd.read_pickle(config.dataset.storage_folder / f'{phase}_beatmaps.pkl') for phase in
                    ['train', 'val', 'test']]
        return datasets
    except FileNotFoundError as e:
        raise FileNotFoundError(f'Check if searching in correct folders. {e}')
