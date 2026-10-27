"""Offline integration tests: real HTTP Range responses and deterministic MKV fixtures."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import struct
import tempfile
import threading
import unittest
import concurrent.futures
import zlib

from extractor import (RangeSource, Matroska, ExtractionError, Cancelled, Track,
                       Cue, render, save_atomic, MIB)


def ebml(eid, data):
    name = eid.to_bytes((eid.bit_length() + 7) // 8, 'big')
    n = next(n for n in range(1, 9) if len(data) < (1 << (7 * n)) - 1)
    return name + ((1 << (7 * n)) | len(data)).to_bytes(n, 'big') + data


def number(eid, n):
    return ebml(eid, n.to_bytes(max(1, (n.bit_length() + 7) // 8), 'big'))


def fixture(codec='S_TEXT/UTF8', compression=None, unknown=False, default=False):
    info = ebml(0x1549A966, number(0x2AD7B1, 1000000))
    track = number(0xD7, 2) + number(0x83, 17) + ebml(0x86, codec.encode()) + ebml(0x22B59C, b'por')
    if default:
        track += number(0x23E383, 1000000000)
    if compression is not None:
        comp = number(0x4254, compression)
        if compression == 3:
            comp += ebml(0x4255, b'PREFIX ')
        track += ebml(0x6D80, ebml(0x6240, ebml(0x5034, comp)))
    tracks = ebml(0x1654AE6B, ebml(0xAE, track))
    clusters = []
    for i, text in enumerate(['Olá, mundo!', 'Segunda fala.']):
        data = number(0xE7, i * 5000 + 1000)
        data += ebml(0xA3, b'\x81\0\0\x80' + b'V' * (256 * 1024))
        payload = text.encode()
        if compression == 0:
            payload = zlib.compress(payload)
        block = b'\x82' + struct.pack('>h', -100) + b'\0' + payload
        data += ebml(0xA3, block) if default else ebml(0xA0, ebml(0xA1, block) + number(0x9B, 1500))
        data += ebml(0xA3, b'\x81\0\0\x80' + b'V' * (256 * 1024))
        clusters.append(b'\x1f\x43\xb6\x75\xff' + data if unknown else ebml(0x1F43B675, data))
    return ebml(0x1A45DFA3, ebml(0x4282, b'matroska')) + ebml(0x18538067, info + tracks + b''.join(clusters))


def indexed_fixture(frame_count=2, wrong_track=False):
    from extractor import children, MemorySource, element
    raw = fixture()
    source = MemorySource(raw)
    header = element(source, 0, source.size)
    seg = element(source, header.end, source.size)
    items = list(children(raw[seg.data:seg.end]))
    info = ebml(items[0][0], items[0][1])
    entry = list(children(items[1][1]))[0][1] + number(0x73C5, 42)
    tracks = ebml(0x1654AE6B, ebml(0xAE, entry))
    tags = ebml(0x1254C367, ebml(0x7373, ebml(0x63C0, number(0x63C5, 42)) +
             ebml(0x67C8, ebml(0x45A3, b'NUMBER_OF_FRAMES') + ebml(0x4487, str(frame_count).encode()))))
    def seek(cues_pos, tags_pos):
        return ebml(0x114D9B74, b''.join(ebml(0x4DBB, ebml(0x53AB, eid.to_bytes(4, 'big')) +
                    ebml(0x53AC, pos.to_bytes(8, 'big'))) for eid, pos in
                    [(0x1C53BB6B, cues_pos), (0x1254C367, tags_pos)]))
    pos = len(seek(0, 0)) + len(info) + len(tracks)
    points = []
    clusters = b''
    for i, (eid, body) in enumerate(items[2:]):
        memory = MemorySource(body)
        offset = 0
        while offset < len(body):
            group = element(memory, offset, len(body))
            if group.id == 0xA0:
                break
            offset = group.end
        if wrong_track:
            body = body[:group.data + 2] + b'\x83' + body[group.data + 3:]
        points.append(ebml(0xBB, number(0xB3, 900 + i * 5000) + ebml(0xB7,
                      number(0xF7, 2) + number(0xF1, pos) + number(0xF0, offset))))
        cluster = ebml(eid, body)
        clusters += cluster
        pos += len(cluster)
    cues = ebml(0x1C53BB6B, b''.join(points))
    return raw[:header.end] + ebml(0x18538067, seek(pos, pos + len(cues)) + info + tracks + clusters + cues + tags)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def do_GET(self):
        if self.path == '/redirect':
            self.send_response(302)
            self.send_header('Location', '/video')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        data = self.server.data
        if self.path == '/no-range':
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.close_connection = True
            return
        a, b = map(int, re.fullmatch(r'bytes=(\d+)-(\d+)', self.headers['Range']).groups())
        b = min(b, len(data) - 1)
        body = data[a:b + 1]
        self.send_response(206)
        self.send_header('Content-Range', f'bytes {a + (1 if self.path == "/wrong" else 0)}-{b}/{len(data)}')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('ETag', self.server.etag)
        self.end_headers()
        self.wfile.write(body)
        self.server.sent += len(body)
    def log_message(self, *args):
        pass


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.data = fixture()
        self.server.sent = 0
        self.server.etag = '"test-1"'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.sources = []

    def tearDown(self):
        for source in self.sources:
            source.close()
        self.server.shutdown()
        self.server.server_close()

    def source(self, path='/video'):
        source = RangeSource(self.url + path)
        self.sources.append(source)
        return source

    def extract(self, **kwargs):
        self.server.data = fixture(**kwargs)
        src = self.source()
        media = Matroska(src)
        tracks = media.inspect()
        cues = media.extract(tracks[0])
        return src, tracks[0], cues

    def test_selective_extraction_and_exact_timing(self):
        src, track, cues = self.extract()
        self.assertEqual([(c.start, c.end, c.text) for c in cues],
                         [(900, 2400, 'Olá, mundo!'), (5900, 7400, 'Segunda fala.')])
        self.assertLess(src.transferred, src.size * 0.03)
        self.assertIn('00:00:00,900 --> 00:00:02,400', render(cues, track))

    def test_unknown_cluster_size(self):
        _, _, cues = self.extract(unknown=True)
        self.assertEqual(len(cues), 2)

    def test_zlib(self):
        _, _, cues = self.extract(compression=0)
        self.assertEqual(cues[0].text, 'Olá, mundo!')

    def test_header_stripping(self):
        _, _, cues = self.extract(compression=3)
        self.assertEqual(cues[0].text, 'PREFIX Olá, mundo!')

    def test_simple_block_default_duration(self):
        _, _, cues = self.extract(default=True)
        self.assertEqual(cues[0].end, 1900)

    def test_reject_full_response_without_reading_video(self):
        with self.assertRaisesRegex(ExtractionError, 'HTTP 200'):
            self.source('/no-range')

    def test_reject_wrong_range(self):
        with self.assertRaisesRegex(ExtractionError, 'intervalo diferente'):
            self.source('/wrong')

    def test_redirect(self):
        self.assertEqual(self.source('/redirect').read(0, 4), b'\x1aE\xdf\xa3')

    def test_content_change(self):
        src = self.source()
        self.server.etag = '"test-2"'
        with self.assertRaisesRegex(ExtractionError, 'conteúdo.*mudou'):
            src.read(100, 10)

    def test_budget(self):
        src = self.source()
        src.limit = 20
        with self.assertRaisesRegex(ExtractionError, 'Limite'):
            src.read(100, 100)
        self.assertEqual(src.transferred, 16)

    def test_percentage_cap(self):
        src = self.source()
        with self.assertRaisesRegex(ExtractionError, 'Limite'):
            src.read(0, src.size)

    def test_cancel(self):
        src = self.source()
        src.cancel.set()
        with self.assertRaises(Cancelled):
            src.read(100, 10)

    def test_parallel_readers_share_one_budget(self):
        src = self.source()
        src.limit = 80
        peers = [src.fork() for _ in range(4)]
        barrier = threading.Barrier(4)
        def fetch(pair):
            i, peer = pair
            barrier.wait()
            try:
                return len(peer.read(200 + i * 100, 40))
            except ExtractionError:
                return 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            sizes = list(pool.map(fetch, enumerate(peers)))
        self.assertEqual(sum(sizes), 40)
        self.assertEqual(src.transferred, 56)
        self.assertEqual(src._budget.reserved, 0)

    def test_image_subtitles_not_claimed_as_text(self):
        self.server.data = fixture(codec='S_HDMV/PGS')
        media = Matroska(self.source())
        t = media.inspect()[0]
        self.assertFalse(t.usable)
        with self.assertRaises(ExtractionError):
            media.extract(t)

    def test_ass_export(self):
        t = Track(1, 'S_TEXT/ASS', private=b'[Script Info]\nScriptType: v4.00+\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n')
        cues = [Cue(1000, 3500, '0,0,Default,,0,0,0,,{\\i1}Olá{\\i0}\\Nmundo!')]
        self.assertIn('Olá\nmundo!', render(cues, t))
        self.assertIn('Dialogue: 0,0:00:01.00,0:00:03.50,Default', render(cues, t, 'ass'))

    def test_atomic_utf8_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'legenda.srt'
            save_atomic(path, 'Olá\n')
            self.assertEqual(path.read_bytes(), b'\xef\xbb\xbfOl\xc3\xa1\r\n')
            self.assertEqual(len(list(Path(directory).iterdir())), 1)

    def test_complete_index_skips_video_and_preserves_timing(self):
        self.server.data = indexed_fixture()
        src = self.source()
        media = Matroska(src)
        track = media.inspect()[0]
        cues = media.extract(track)
        self.assertIn('índice', media.method)
        self.assertEqual([(c.start, c.end, c.text) for c in cues],
                         [(900, 2400, 'Olá, mundo!'), (5900, 7400, 'Segunda fala.')])
        self.assertLess(src.transferred, 4096)

    def test_incomplete_index_falls_back_to_scan(self):
        self.server.data = indexed_fixture(frame_count=3)
        media = Matroska(self.source())
        cues = media.extract(media.inspect()[0])
        self.assertEqual(len(cues), 2)
        self.assertIn('Varredura', media.method)

    def test_index_wrong_track_is_not_silently_saved(self):
        self.server.data = indexed_fixture(wrong_track=True)
        media = Matroska(self.source())
        track = media.inspect()[0]
        with self.assertRaisesRegex(ExtractionError, 'não corresponde'):
            media.extract(track)

    def test_actual_ffmpeg_fixture_if_available(self):
        fixture_path = Path(__file__).resolve().parents[3] / 'work' / 'test-media.mkv'
        if not fixture_path.exists():
            self.skipTest('Generate work/test-media.mkv for FFmpeg integration.')
        self.server.data = fixture_path.read_bytes()
        src = self.source()
        media = Matroska(src)
        tracks = media.inspect()
        cues = media.extract(tracks[0])
        self.assertEqual([(round(c.start), round(c.end), c.text) for c in cues],
                         [(1000, 3500, 'Olá, mundo!'), (15000, 18250, 'Legenda com acentuação: ação.'),
                          (50000, 53000, 'Última fala.')])
        self.assertLess(src.transferred, src.size * 0.35)
        print(f'\nReal MKV: {len(cues)} cues; {src.transferred}/{src.size} bytes ({src.transferred / src.size:.2%})')


if __name__ == '__main__':
    unittest.main(verbosity=2)
