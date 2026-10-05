"""Offline tests for the Xbox app save store (wgs.py) and the mirror sync (Ctx.xbox_sync).

    python test_wgs.py [path to Z1ni's XGP-save-extractor main.py]   (optional cross-check)

Builds a fake %LOCALAPPDATA% with a WGS store laid out like the game's, then checks: the index round-trips
byte for byte, slots are mirrored as .sav files, a copy the tool writes goes into the store only while the
game is closed (new container: flag 5, seq 1; existing: same folder, seq + 1, old files removed, backup made),
a played copy is mirrored back, and an independent parser reads the result.
"""
import importlib.util
import json
import os
import shutil
import struct
import sys
import tempfile
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix='ohs-wgs-'))
os.environ['LOCALAPPDATA'] = str(TMP / 'Local')
sys.path.insert(0, str(HERE))
import wgs  # noqa: E402
import offline_hero_sync as ohs  # noqa: E402

PASSED = []


def check(cond, what):
    if not cond:
        raise SystemExit('FAIL ' + what)
    PASSED.append(what)
    print('PASS', what)


def save_json(cid, level, online=False):
    return json.dumps({'SerializeMeta': {'InternalVersion': 0, 'HardFormat': 'FCharacterSaveV1', 'SoftVersion': 5,
                                         'FormatHash': 1764133460},
                       'CharacterSaveV1': {'MetaData': {'CharacterId': cid, 'Created': 0, 'GameDataUpdated': 1,
                                                        'IsOnline': online, 'IsGuest': False, 'Level': level,
                                                        'PowerLevel': level}}},
                      separators=(',', ':')).encode()


def game_container(user, name, data, cloud=True):
    """A container the way the game/app leaves it (cloud id, flag 7 like synced ones, seq 3)."""
    c = wgs.Container()
    c.name, c.cloud_id, c.seq, c.flag, c.reserved = name, ('0x%016X' % uuid.uuid4().int)[:18] if cloud else '', \
        3, 7 if cloud else 5, 0
    c.guid = uuid.uuid4().bytes
    folder = c.folder(user)
    folder.mkdir(parents=True)
    fid = uuid.uuid4()
    (folder / fid.bytes_le.hex().upper()).write_bytes(data)
    (folder / 'container.3').write_bytes(struct.pack('<II', 4, 1) + 'Data'.encode('utf-16-le').ljust(128, b'\0') +
                                         fid.bytes + fid.bytes)
    c.mtime, c.size = wgs.now_filetime(), len(data)
    return c


def main():
    user = Path(os.environ['LOCALAPPDATA']) / 'Packages' / wgs.PACKAGE / 'SystemAppData' / 'wgs' / \
        '0009000007CF0AD7_0000000000000000000000006B9DE498'
    user.mkdir(parents=True)
    played = 'aaaaaaaa-0000-11f1-8000-000000000001'
    idx_bytes = [struct.pack('<II', 0xE, 0), wgs._ws(''), wgs._ws('Microsoft.MinecraftDungeons2_8wekyb3d8bbwe!Game'),
                 struct.pack('<QI', wgs.now_filetime(), 0x1), wgs._ws('"0x8DE1234567890AB"'), struct.pack('<Q', 0)]
    (user / 'containers.index').write_bytes(b''.join(idx_bytes))
    idx = wgs.Index(user)
    idx.containers = [game_container(user, 'GlobalSaveDataDefault', b'{"global":1}'),
                      game_container(user, 'Character' + played, save_json(played, 7))]
    idx.write()
    raw = (user / 'containers.index').read_bytes()
    check(wgs.Index(user).to_bytes() == raw, 'containers.index parses and writes back byte for byte')

    check(ohs.Ctx(data_dir=TMP / 'tool').wgs is None or ohs.XBOX_AUTO,
          'without --xbox the Xbox store is only picked by itself when XBOX_AUTO is on')
    ctx = ohs.Ctx(data_dir=TMP / 'tool', xbox=True)
    check(ctx.wgs is not None and ctx.save_dir == TMP / 'tool' / 'xbox' / user.name / 'SaveGames',
          'with --xbox the Xbox store is used, through a mirror')
    ctx.xbox_sync(game_running=False)
    check((ctx.save_dir / ('Character%s.sav' % played)).read_bytes() == save_json(played, 7) and
          (ctx.save_dir / 'GlobalSaveDataDefault.sav').read_bytes() == b'{"global":1}',
          'every slot is mirrored as <slot>.sav')

    # the tool writes a new offline copy into the mirror (what write_offline does), game running
    new_id = 'bbbbbbbb-0000-11f1-8000-000000000002'
    data = save_json(new_id, 100)
    (ctx.save_dir / ('Character%s.sav' % new_id)).write_bytes(data)
    st = ctx.load_state()
    st['heroes']['cccccccc-0000-11f1-8000-000000000003'] = {
        'offline_id': new_id, 'written_hash': ohs.content_hash(json.loads(data))}
    ctx.save_state(st)
    check(ctx.xbox_sync(game_running=True) == [] and 'Character' + new_id not in wgs.Store(user).slots(),
          'while the game runs nothing is written into its saves')
    check(ctx.xbox_sync(game_running=False) == ['Character' + new_id], 'after it closes the copy is written')
    slots = wgs.Store(user).slots()
    c, files = slots['Character' + new_id]
    check(c.flag == 5 and c.seq == 1 and c.cloud_id == '' and files[0][1].read_bytes() == data and
          files[0][0] == 'Data', 'new container: local (flag 5), seq 1, file named like the game\'s ("Data")')
    check(ctx.xbox_sync(game_running=False) == [], 'nothing pending: no second write')

    # update: the online hero changed, a new copy is written over the existing container
    data2 = save_json(new_id, 101)
    (ctx.save_dir / ('Character%s.sav' % new_id)).write_bytes(data2)
    st = ctx.load_state()
    h = st['heroes']['cccccccc-0000-11f1-8000-000000000003']
    h['written_hash'] = ohs.content_hash(json.loads(data2))
    ctx.save_state(st)
    folder_before = c.folder(user)
    ctx.xbox_sync(game_running=False)
    c2, files2 = wgs.Store(user).slots()['Character' + new_id]
    left = sorted(p.name for p in folder_before.iterdir())
    check(c2.guid == c.guid and c2.seq == 2 and files2[0][1].read_bytes() == data2 and
          left == sorted(['container.2', files2[0][1].name]),
          'update: same folder, seq + 1, old files removed')
    check(any((ctx.backup_dir / 'xbox').glob('wgs-*-Character' + new_id)), 'the container was backed up first')

    # the player plays the copy: the game writes the container; the mirror picks it up, nothing goes back
    played_data = save_json(new_id, 102)
    wgs.Store(user).write('Character' + new_id, played_data)
    ctx.xbox_sync(game_running=False)
    check((ctx.save_dir / ('Character%s.sav' % new_id)).read_bytes() == played_data and
          wgs.Store(user).read('Character' + new_id) == played_data,
          'a copy played in the game is mirrored back and left alone')

    # untouched slots stay byte-identical
    check(wgs.Store(user).read('Character' + played) == save_json(played, 7) and
          wgs.Store(user).read('GlobalSaveDataDefault') == b'{"global":1}', 'other saves are never written')

    # independent parser (Z1ni/XGP-save-extractor) reads the store
    if len(sys.argv) > 1 and Path(sys.argv[1]).is_file():
        spec = importlib.util.spec_from_file_location('xgp', sys.argv[1])
        xgp = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(xgp)
        except SystemExit:
            pass
        _, conts = xgp.read_user_containers(user)
        by = {x['name']: x for x in conts}
        check(by['Character' + new_id]['files'][0]['path'].read_bytes() == played_data and
              by['GlobalSaveDataDefault']['files'][0]['path'].read_bytes() == b'{"global":1}',
              'XGP-save-extractor reads the containers this tool wrote')
    print('\n%d checks passed' % len(PASSED))
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == '__main__':
    main()
