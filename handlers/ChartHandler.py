#!/usr/bin/env python3
"""ChartHandler
Provides timechart command execution using StatsHandler and GeneralHandler."""
import logging
import pandas as pd
from handlers.StatsHandler import StatsHandler
from handlers.GeneralHandler import GeneralHandler

logger = logging.getLogger(__name__)

class ChartHandler:
    def __init__(self):
        self.stats_handler = StatsHandler()
        self.general_handler = GeneralHandler()

    def run_timechart(self, tokens, df):
        """Execute a timechart command.
        tokens: list like ['timechart','span=1h','count','by','field']
        df: pandas DataFrame
        Returns aggregated DataFrame with _time binned to the provided span."""
        if not isinstance(df, pd.DataFrame):
            logging.error(f"[x] Input is not a DataFrame: {type(df)}")
            raise TypeError("df must be a pandas DataFrame")

        span = '1h'
        idx = 1
        if len(tokens) > 1 and tokens[1].lower().startswith('span='):
            span = tokens[1].split('=', 1)[1]
            idx += 1
        # normalize span to pandas offset aliases
        span = span.lower()
        unit_map = {
            'seconds': 's', 'second': 's',
            'minutes': 'min', 'minute': 'min',
            'hours': 'h', 'hour': 'h',
            'days': 'd', 'day': 'd',
            'weeks': 'w', 'week': 'w',
            'years': 'y', 'year': 'y',
        }
        for word, abbr in unit_map.items():
            if span.endswith(word):
                span = span[:-len(word)] + abbr
                break

        stats_tokens = ['stats'] + tokens[idx:]
        lower_tokens = [t.lower() for t in stats_tokens]
        if 'by' in lower_tokens:
            by_idx = lower_tokens.index('by')
            stats_tokens.insert(by_idx + 1, '_time')
        else:
            stats_tokens += ['by', '_time']

        binned_df = self.general_handler.execute_bin(df.copy(), '_time', span)
        return self.stats_handler.run_stats(stats_tokens, binned_df)
