import io
import json
import os
import sqlite3
import tempfile
import unittest
from urllib.error import HTTPError
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

# Keep tests completely separate from any real development user database.
_IMPORT_TMP = tempfile.TemporaryDirectory()
os.environ['XAU_DB_PATH'] = str(Path(_IMPORT_TMP.name) / 'import.sqlite3')
os.environ['APP_SECRET'] = 'unit-test-secret-not-for-deployment'
os.environ.pop('TWELVE_DATA_API_KEY', None)

import app as server
import market
_IMPORT_TMP.cleanup()

NOW = 1_800_000_030


class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        server.DB_PATH = Path(self.tmp.name) / 'test.sqlite3'
        server.API_KEY = ''
        server._cache.clear()
        server._provider_errors.clear()
        server._attempts.clear()
        server.init_db()
        server.app.config.update(TESTING=True)
        self.client = server.app.test_client()
        self.csrf = self.client.get('/api/session').get_json()['csrf']

    def tearDown(self):
        self.tmp.cleanup()

    def post(self, path, data, csrf=None):
        return self.client.post(path, json=data, headers={'X-CSRF-Token': self.csrf if csrf is None else csrf})

    def register(self):
        response = self.post('/api/auth/register', {'name': 'Gold Trader', 'email': 'Trader@Example.com', 'password': 'a-strong-password'})
        self.assertEqual(response.status_code, 201)
        self.csrf = response.get_json()['csrf']
        return response

    def test_public_demo_and_member_gate(self):
        self.assertEqual(self.client.get('/api/market').status_code, 401)
        response = self.client.get('/api/demo-market?interval=15m')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data['source']['mode'], 'demo')
        self.assertEqual(data['symbol'], 'XAU/USD')
        self.assertEqual(len(data['candles']), 150)
        self.assertIn(data['analysis']['signal'], ('BUY', 'SELL', 'WAIT'))
        self.assertEqual(self.client.get('/api/demo-market?interval=bad').status_code, 400)
        self.register()
        self.assertEqual(self.client.get('/api/market').get_json()['source']['mode'], 'demo')

    def test_registration_login_logout_and_password_hash(self):
        self.assertEqual(self.post('/api/auth/register', {}, csrf='wrong').status_code, 403)
        self.assertEqual(self.post('/api/auth/register', {'name': 'A', 'email': 'bad', 'password': 'short'}).status_code, 400)
        self.register()
        with closing(sqlite3.connect(server.DB_PATH)) as conn:
            saved = conn.execute('SELECT password_hash FROM users').fetchone()[0]
        self.assertNotEqual(saved, 'a-strong-password')
        self.assertTrue(saved.startswith('scrypt:'))
        self.assertEqual(self.client.get('/api/session').get_json()['user']['email'], 'trader@example.com')
        duplicate = self.post('/api/auth/register', {'name': 'Other Person', 'email': 'TRADER@example.com', 'password': 'a-strong-password'})
        self.assertEqual(duplicate.status_code, 409)
        out = self.post('/api/auth/logout', {})
        self.csrf = out.get_json()['csrf']
        self.assertEqual(self.client.get('/api/market').status_code, 401)
        self.assertEqual(self.post('/api/auth/login', {'email': 'trader@example.com', 'password': 'wrong'}).status_code, 401)
        login = self.post('/api/auth/login', {'email': 'TRADER@example.com', 'password': 'a-strong-password'})
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.get_json()['user']['name'], 'Gold Trader')

    def test_provider_failure_is_not_silently_replaced_by_demo(self):
        server.API_KEY = 'secret-should-never-be-returned'
        self.register()
        with patch.object(market, 'twelve_data_candles', side_effect=market.MarketDataError('Provider unavailable.')):
            response = self.client.get('/api/market?interval=5m')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(server.API_KEY, response.get_data(as_text=True))
        self.assertEqual(self.client.get('/api/demo-market?interval=5m').get_json()['source']['mode'], 'demo')

    def test_provider_errors_are_temporarily_cached_not_retried_per_viewer(self):
        server.API_KEY = 'one-key'
        with patch.object(market, 'twelve_data_candles', side_effect=market.MarketDataError('Quota reached')) as fetch, \
             patch.object(server.time, 'time', side_effect=[NOW, NOW + 1]):
            for _ in range(2):
                with self.assertRaises(market.MarketDataError) as caught:
                    server.get_snapshot('1m', False)
                self.assertEqual(str(caught.exception), 'Quota reached')
        self.assertEqual(fetch.call_count, 1)

    def test_forming_bar_does_not_drive_or_advance_cached_signal(self):
        server.API_KEY = 'one-key'
        old_cache_seconds = server.CACHE_SECONDS
        server.CACHE_SECONDS = 3600
        base = (NOW // 60) * 60
        bars = [{**bar, 'closed': True} for bar in market.demo_candles('1m', now=base)]
        last_close = bars[-1]['close']
        forming = {'time': market._iso(base), 'open': last_close, 'high': last_close + 10,
                   'low': last_close - 1, 'close': last_close + 9, 'closed': False}
        try:
            with patch.object(market, 'twelve_data_candles', return_value=bars + [forming]) as fetch, \
                 patch.object(server.time, 'time', side_effect=[base + 10, base + 75, base + 130]):
                first = server.get_snapshot('1m', False)
                later = server.get_snapshot('1m', False)
                stale = server.get_snapshot('1m', False)
            self.assertTrue(first['source']['forming'])
            self.assertEqual(first['latest']['closed_price'], last_close)
            self.assertEqual(first['latest']['price'], last_close + 9)
            self.assertFalse(first['candles'][-1]['closed'])
            self.assertFalse(later['source']['forming'])
            self.assertTrue(later['source']['stale'])
            self.assertEqual(later['analysis']['signal'], 'WAIT')
            self.assertEqual(later['source']['last_closed_at'], first['source']['last_closed_at'])
            self.assertEqual(later['latest']['price'], last_close)
            self.assertTrue(stale['source']['stale'])
            self.assertEqual(stale['analysis']['signal'], 'WAIT')
            self.assertEqual(stale['recent_signals'], [])
            self.assertEqual(fetch.call_count, 1)
        finally:
            server.CACHE_SECONDS = old_cache_seconds

    def test_cached_provider_signal_becomes_stale(self):
        server.API_KEY = 'key'
        old_cache_seconds = server.CACHE_SECONDS
        server.CACHE_SECONDS = 3600
        timestamp = (NOW // 300) * 300
        candles = market.demo_candles('5m', now=timestamp)
        try:
            with patch.object(market, 'twelve_data_candles', return_value=candles) as fetch, patch.object(server.time, 'time', side_effect=[timestamp, timestamp + 1801]):
                first = server.get_snapshot('5m', False)
                second = server.get_snapshot('5m', False)
            self.assertFalse(first['source']['stale'])
            self.assertTrue(second['source']['stale'])
            self.assertEqual(second['analysis']['signal'], 'WAIT')
            self.assertEqual(first['source']['retrieved_at'], second['source']['retrieved_at'])
            self.assertEqual(fetch.call_count, 1)
        finally:
            server.CACHE_SECONDS = old_cache_seconds


class MarketTests(unittest.TestCase):
    def test_demo_timeframes_are_consistent_and_closed(self):
        now = (NOW // 900) * 900 + 10
        small = market.demo_candles('5m', now=now, count=3)
        large = market.demo_candles('15m', now=now, count=1)[0]
        self.assertEqual(large['open'], small[0]['open'])
        self.assertEqual(large['close'], small[-1]['close'])
        self.assertEqual(large['high'], max(c['high'] for c in small))
        self.assertEqual(large['low'], min(c['low'] for c in small))
        self.assertLessEqual(market._parse_timestamp(large['time']) + 900, now)

    def test_one_minute_demo_aggregates_exactly_to_five_minutes(self):
        now = (NOW // 300) * 300 + 10
        one = market.demo_candles('1m', now=now, count=5)
        five = market.demo_candles('5m', now=now, count=1)[0]
        self.assertEqual(market.aggregate_five_minute(one), [five])
        self.assertEqual(market.aggregate_five_minute(one[:-1]), [])
        self.assertEqual(market._parse_timestamp(one[-1]['time']) + 60, (NOW // 300) * 300)

    def test_scalp_buy_sell_rsi_boundaries_and_gaps(self):
        bars = market.demo_candles('1m', now=NOW, count=150)
        bars[-2]['close'], bars[-1]['close'] = 3960.0, 3962.0
        def up(prices, period):
            if len(prices) == len(bars):
                return [3961.0 if period == 9 else 3959.0] * len(prices)
            return [3962.0 if period == 9 else 3960.0] * len(prices)
        with patch.object(market, 'ema', side_effect=up):
            self.assertEqual(market.scalp_setup(bars, 52, False)['signal'], 'BUY')
            self.assertEqual(market.scalp_setup(bars, 68, False)['signal'], 'BUY')
            self.assertEqual(market.scalp_setup(bars, 51.99, False)['signal'], 'WAIT')
            self.assertEqual(market.scalp_setup(bars, 68.01, False)['signal'], 'WAIT')
            self.assertEqual(market.scalp_setup(bars, 60, True)['signal'], 'WAIT')
            self.assertEqual(market.scalp_setup(bars[:-2] + [bars[-1]], 60, False)['signal'], 'WAIT')
        bars[-2]['close'], bars[-1]['close'] = 3962.0, 3960.0
        def down(prices, period):
            if len(prices) == len(bars):
                return [3961.0 if period == 9 else 3963.0] * len(prices)
            return [3960.0 if period == 9 else 3962.0] * len(prices)
        with patch.object(market, 'ema', side_effect=down):
            self.assertEqual(market.scalp_setup(bars, 32, False)['signal'], 'SELL')
            self.assertEqual(market.scalp_setup(bars, 48, False)['signal'], 'SELL')
            self.assertEqual(market.scalp_setup(bars, 31.99, False)['signal'], 'WAIT')
            self.assertEqual(market.scalp_setup(bars, 48.01, False)['signal'], 'WAIT')
            bars[-2]['close'] = 3960.0
            self.assertEqual(market.scalp_setup(bars, 40, False)['signal'], 'WAIT')

    def test_missing_minute_in_partial_group_forces_wait(self):
        now = (NOW // 300) * 300 + 180
        bars = market.demo_candles('1m', now=now, count=150)
        # The most recent two bars are contiguous, but the preceding minute
        # in the same 5m group is missing.
        missing = bars[:-3] + bars[-2:]
        result = market.scalp_setup(missing, 60, False)
        self.assertEqual(result['signal'], 'WAIT')
        self.assertIn('missing', result['reason'])

    def test_recent_entries_are_historical_and_use_no_future_candles(self):
        bars = market.demo_candles('1m', now=NOW, count=150)
        snapshot = market.analyze(bars, '1m', 'demo', now=NOW)
        entries = snapshot['recent_signals']
        self.assertTrue(entries)  # deterministic synthetic fixture, not a market forecast
        self.assertLessEqual(len(entries), 5)
        for entry in entries:
            index = next(i for i, bar in enumerate(bars)
                         if market._parse_timestamp(bar['time']) + 60 == market._parse_timestamp(entry['closed_at']))
            self.assertGreaterEqual(index, len(bars) - 5)
            prefix = bars[:index + 1]
            rsi = market.rsi_wilder([bar['close'] for bar in prefix])
            self.assertEqual(entry['signal'], market.scalp_setup(prefix, rsi, False)['signal'])
            self.assertEqual(entry['price'], prefix[-1]['close'])
        self.assertEqual(snapshot['analysis']['strategy'], 'scalp_1m')
        stale = market.analyze(bars, '1m', 'provider', now=NOW + 121)
        self.assertTrue(stale['source']['stale'])
        self.assertEqual(stale['analysis']['signal'], 'WAIT')
        self.assertEqual(stale['recent_signals'], [])

    def test_indicators_handle_flat_up_and_down_series(self):
        self.assertEqual(market.rsi_wilder([10.0] * 20), 50.0)
        self.assertEqual(market.rsi_wilder(list(range(20))), 100.0)
        self.assertEqual(market.rsi_wilder(list(reversed(range(20)))), 0.0)
        self.assertEqual(market.ema([10, 10, 10], 9), [10.0, 10.0, 10.0])
        bars = [{'high': 11.0, 'low': 9.0, 'close': 10.0} for _ in range(20)]
        self.assertEqual(market.atr_wilder(bars), 2.0)

    def test_signal_boundaries_and_stale_guard(self):
        candles = market.demo_candles('15m', now=NOW, count=40)
        def fake_ema(prices, period):
            return [2.0 if period == 9 else 1.0] * len(prices)
        with patch.object(market, 'ema', side_effect=fake_ema), patch.object(market, 'rsi_wilder', return_value=55.0):
            self.assertEqual(market.analyze(candles, '15m', 'demo', now=NOW)['analysis']['signal'], 'BUY')
            stale = market.analyze(candles, '15m', 'provider', now=NOW + 86400)
            self.assertTrue(stale['source']['stale'])
            self.assertEqual(stale['analysis']['signal'], 'WAIT')
        with patch.object(market, 'ema', side_effect=lambda prices, period: [1.0 if period == 9 else 2.0] * len(prices)), patch.object(market, 'rsi_wilder', return_value=45.0):
            self.assertEqual(market.analyze(candles, '15m', 'demo', now=NOW)['analysis']['signal'], 'SELL')
        with patch.object(market, 'ema', side_effect=fake_ema), patch.object(market, 'rsi_wilder', return_value=54.99):
            self.assertEqual(market.analyze(candles, '15m', 'demo', now=NOW)['analysis']['signal'], 'WAIT')

    def test_provider_adapter_filters_open_and_invalid_bars(self):
        timestamp = (NOW // 300) * 300
        original = market.demo_candles('5m', now=timestamp, count=50)
        rows = [dict(datetime=c['time'], open=str(c['open']), high=str(c['high']), low=str(c['low']), close=str(c['close'])) for c in reversed(original)]
        rows.insert(0, {'datetime': market._iso(timestamp), 'open': '4000', 'high': '4001', 'low': '3999', 'close': '4000'})  # still open
        rows.insert(0, {'datetime': market._iso(timestamp - 99 * 300), 'open': '-1', 'high': '1', 'low': '1', 'close': '1'})
        response = io.BytesIO(json.dumps({'status': 'ok', 'values': rows}).encode())
        with patch.object(market, 'urlopen', return_value=response) as mocked:
            candles = market.twelve_data_candles('5m', 'secret', now=timestamp + 30)
        self.assertEqual(len(candles), 50)
        self.assertEqual(candles[0]['time'], original[0]['time'])
        self.assertEqual(candles[-1]['close'], original[-1]['close'])
        self.assertIn('symbol=XAU%2FUSD', mocked.call_args.args[0].full_url)
        self.assertIn('timezone=UTC', mocked.call_args.args[0].full_url)

    def test_provider_open_bar_is_explicitly_marked_provisional(self):
        timestamp = (NOW // 60) * 60
        bars = market.demo_candles('1m', now=timestamp, count=150)
        rows = [dict(datetime=c['time'], open=str(c['open']), high=str(c['high']), low=str(c['low']), close=str(c['close'])) for c in reversed(bars)]
        rows.insert(0, {'datetime': market._iso(timestamp), 'open': '4000', 'high': '4001', 'low': '3999', 'close': '4000'})
        response = io.BytesIO(json.dumps({'values': rows}).encode())
        with patch.object(market, 'urlopen', return_value=response):
            candles = market.twelve_data_candles('1m', 'key', now=timestamp + 10, include_open=True)
        self.assertEqual(len(candles), 151)
        self.assertTrue(all(c['closed'] for c in candles[:-1]))
        self.assertFalse(candles[-1]['closed'])
        snapshot = market.analyze(candles[:-1], '1m', 'provider', now=timestamp + 10)
        before = dict(snapshot['analysis'])
        market.append_forming_candle(snapshot, candles[-1])
        self.assertEqual(snapshot['analysis'], before)
        self.assertTrue(snapshot['source']['forming'])
        self.assertFalse(snapshot['candles'][-1]['closed'])
        self.assertEqual(snapshot['latest']['price'], 4000)
        self.assertEqual(snapshot['latest']['closed_price'], candles[-2]['close'])

    def test_provider_quota_errors_are_clear_and_secret_free(self):
        for payload in ({'status': 'error', 'code': 429, 'message': 'secret'},):
            with patch.object(market, 'urlopen', return_value=io.BytesIO(json.dumps(payload).encode())) as fetch:
                with self.assertRaises(market.MarketDataError) as caught:
                    market.twelve_data_candles('1m', 'secret', now=NOW)
                self.assertIn('limit', str(caught.exception))
                self.assertNotIn('secret', str(caught.exception))
                self.assertEqual(fetch.call_count, 1)
        error = HTTPError('https://api.twelvedata.com/?apikey=secret', 429, 'rate limit', {}, None)
        with patch.object(market, 'urlopen', side_effect=error) as fetch:
            with self.assertRaises(market.MarketDataError) as caught:
                market.twelve_data_candles('1m', 'secret', now=NOW)
            self.assertIn('limit', str(caught.exception))
            self.assertNotIn('secret', str(caught.exception))
            self.assertEqual(fetch.call_count, 1)

    def test_provider_error_is_sanitized(self):
        response = io.BytesIO(json.dumps({'status': 'error', 'message': 'key secret rejected'}).encode())
        with patch.object(market, 'urlopen', return_value=response):
            with self.assertRaises(market.MarketDataError) as caught:
                market.twelve_data_candles('1h', 'secret', now=NOW)
        self.assertNotIn('secret', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
