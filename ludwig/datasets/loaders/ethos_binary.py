# Copyright (c) 2022 Predibase, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
import pandas as pd

from ludwig.datasets.loaders.dataset_loader import DatasetLoader


class EthosBinaryLoader(DatasetLoader):
    def load_file_to_dataframe(self, file_path: str) -> pd.DataFrame:
        # This dataset uses ; seperator instead of ,
        return pd.read_csv(file_path, sep=";")

    def transform_dataframe(self, dataframe: pd.DataFrame) -> pd.DataFrame:
        processed_df = super().transform_dataframe(dataframe)
        # convert float labels (0.0, 1.0) to binary labels
        processed_df["isHate"] = processed_df["isHate"].astype(int)
        return processed_df
