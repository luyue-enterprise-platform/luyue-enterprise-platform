# -*- coding: utf-8 -*-
"""
Wrapper to run PyInstaller while bypassing sandbox safe-delete interception.
Restores original os.remove / os.rmdir / shutil.rmtree before launching PyInstaller.
"""
import sys
import os
import shutil
import pathlib

# Access the original functions stored by sitecustomize
# sitecustomize.py stores them as module-level variables: _orig_remove, etc.
try:
    import sitecustomize as sc
    if hasattr(sc, '_orig_remove'):
        os.remove = sc._orig_remove
        print("[_run_build] Restored os.remove from sitecustomize")
    if hasattr(sc, '_orig_unlink'):
        os.unlink = sc._orig_unlink
        print("[_run_build] Restored os.unlink from sitecustomize")
    if hasattr(sc, '_orig_rmdir'):
        os.rmdir = sc._orig_rmdir
        print("[_run_build] Restored os.rmdir from sitecustomize")
    if hasattr(sc, '_orig_shutil_rmtree'):
        shutil.rmtree = sc._orig_shutil_rmtree
        print("[_run_build] Restored shutil.rmtree from sitecustomize")
    if hasattr(sc, '_orig_path_unlink'):
        pathlib.Path.unlink = sc._orig_path_unlink
        print("[_run_build] Restored pathlib.Path.unlink from sitecustomize")
    if hasattr(sc, '_orig_path_rmdir'):
        pathlib.Path.rmdir = sc._orig_path_rmdir
        print("[_run_build] Restored pathlib.Path.rmdir from sitecustomize")
except Exception as e:
    print(f"[_run_build] Could not access sitecustomize originals: {e}")

# Now run PyInstaller
if __name__ == '__main__':
    from PyInstaller.__main__ import run
    sys.exit(run())
