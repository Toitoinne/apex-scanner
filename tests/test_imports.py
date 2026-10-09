"""Chaque module du bot doit au moins se charger : une erreur de syntaxe dans un service que les autres
tests n'importent pas (ex. le superviseur) ne doit jamais atteindre la production."""
import importlib
import pkgutil

import apex


def test_every_module_imports():
    failed = []
    for m in pkgutil.walk_packages(apex.__path__, "apex."):
        try:
            importlib.import_module(m.name)
        except Exception as e:  # noqa: BLE001
            failed.append(f"{m.name}: {type(e).__name__}: {e}")
    assert not failed, "\n".join(failed)
