#!/usr/bin/env python3
import pandas as pd
import logging

class MultiSearchHandler:
    """Execute multiple subsearches and combine their results."""

    def run_multisearch(self, subsearch_tokens_list, process_func):
        dfs = []
        for tokens in subsearch_tokens_list:
            try:
                df = process_func(tokens)
                dfs.append(df)
            except Exception as e:
                logging.error(f"[x] MULTISEARCH subsearch failure: {e}")
        if not dfs:
            return pd.DataFrame()
        return pd.concat(dfs, ignore_index=True)
