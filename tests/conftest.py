"""Fixtures compartidas de la suite.

Los tests NUNCA tocan la base real del bot (`bottin_history.db`): cada test
recibe una SQLite nueva en su propio `tmp_path` vía el fixture `db`.
Tampoco se hace red: todo lo que se prueba es local (funciones puras, RAG
sobre los PDFs de `docs/` y SQLite).
"""
import os
import sys

import pytest

# La raíz del proyecto (donde vive bot.py) tiene que estar en el path antes
# de importar el módulo.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot  # noqa: E402  (el import va después del sys.path)


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Base de SQLite aislada: crea las tablas en un archivo temporal y
    redirige `bot.DB_NAME` hacia ahí para toda la duración del test."""
    ruta = tmp_path / "bottin_test.db"
    monkeypatch.setattr(bot, "DB_NAME", str(ruta))
    bot.init_db()
    bot.init_stats_db()
    return ruta
