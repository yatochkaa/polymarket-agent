#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_fast.py -- uskoritel' progona okna BEZ pravki collect_window_v1.py.

Priyom (tot zhe, chto monkey-patch v Dif 2): importiruem collect_window_v1
kak modul' i podmenyaem V PAMYATI TOL'KO load_repull_manifest na versiyu s
process-local keshem. Fajl collect_window_v1.py NE izmenyaetsya ni na bayt --
hesh ostayotsya fae5999cc9eebeae26087bfedf449a86da1156f8c566ae6976c5d2a937543d73.

PRICHINA (po kodu fae5999c):
  read_trades_raw_win (str. 1230) zovyot load_repull_manifest na KAZHDYY rynok,
  a load_repull_manifest (str. 1213) schitaet _sha256_file KAZHDOGO fajla
  manifesta -> O(N^2) = 4068^2 ~ 16.55 mln sha256 (~14.2 TB). Kesh svodit eto
  k odnomu prohodu validacii = N = 4068 sha256.

RAZRESHYONNYE REZHIMY (tol'ko chtenie s diska; manifest za progon neizmenen):
    verify-passa | cascade-probe | run
repull / dryrun / selftest ZAPRESHCHENY: oni MENYAYUT manifest cherez
append_repull_manifest (str. 1307 v repull, 1745+ v selftest), i kesh vernul
by ustarevshiy snimok -> yavnyy otkaz s kodom vyhoda 2.

ZAPUSK (iz kornya proekta, ryadom s collect_window_v1.py):
    python run_fast.py verify-passa
    python run_fast.py cascade-probe
    python run_fast.py run --p11 --source=raw_win

Vse flagi (--data-dir, --p11, --source=...) razbiraet shtatnyy _arg/main
iz collect_window_v1: sys.argv peredayotsya bez izmeneniy.
"""

import os
import sys

import collect_window_v1 as cw

# --- process-local kesh manifesta; klyuch (abspath(data_dir), repull_dir) ---
_MANIFEST_CACHE = {}
_orig_load_repull_manifest = cw.load_repull_manifest  # ORIGINAL: v nyom zhivyot sha-validaciya


def _cached_load_repull_manifest(data_dir, repull_dir=cw.REPULL_DIR_NAME):
    """Odnokratno vyzyvaet ORIGINAL'nyy load_repull_manifest (polnaya sha-validaciya
    kazhdogo fajla, collect_window_v1.py str. 1209-1214) i keshiruet ITOGOVYY slovar'
    na vremya zhizni processa. Klyuch -- (abspath(data_dir), repull_dir).

    sha256 NE otklyuchaetsya: ona prosto vypolnyaetsya odin raz na fajl (v pervom
    vyzove dlya klyucha), a ne odin raz na fajl na KAZHDYY rynok. Podmena/bityj/
    otsutstvuyushchiy fajl vsyo ravno vypadaet iz slovarya na str. 1213 ->
    read_trades_raw_win (str. 1231-1232) brosaet AmbiguousInput.
    """
    key = (os.path.abspath(data_dir), repull_dir)
    cached = _MANIFEST_CACHE.get(key)
    if cached is None:
        cached = _orig_load_repull_manifest(data_dir, repull_dir)  # real'naya validaciya sha
        _MANIFEST_CACHE[key] = cached
    return cached


_ALLOWED = ("verify-passa", "cascade-probe", "run")


def _install_patch():
    # read_trades_raw_win (str. 1230) i repull razreshayut imya load_repull_manifest
    # v globalah modulya cw v MOMENT vyzova, poetomu perepriviazka atributa modulya
    # pereklyuchaet vse vnutrennie vyzovy na keshiruyushchuyu versiyu.
    cw.load_repull_manifest = _cached_load_repull_manifest


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    mode = sys.argv[1]
    if mode not in _ALLOWED:
        sys.stderr.write(
            "run_fast: rezhim '%s' ZAPRESHCHYON. Kesh manifesta bezopasen tol'ko dlya\n"
            "rezhimov chteniya s diska (manifest za progon ne menyaetsya).\n"
            "Razresheno: %s.\n"
            "Dlya repull/dryrun/selftest zapuskay obychnyy: python collect_window_v1.py %s\n"
            % (mode, " | ".join(_ALLOWED), mode)
        )
        sys.exit(2)

    _install_patch()
    print("[run_fast] O(N^2)->O(N) kesh manifesta AKTIVEN; collect_window_v1.py NE izmenyon.")
    print("[run_fast] rezhim=%s | kesh-klyuch=(abspath(data_dir),repull_dir) | sha256: odin prohod na fajl." % mode)
    # Delegiruem v shtatnyy dispetcher fae5999c: sys.argv uzhe soderzhit mode i flagi.
    cw.main()


if __name__ == "__main__":
    main()
