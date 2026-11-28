"""Historial de partidos en SQLite (racha y estadísticas).

Todos los tests corren sobre una base temporal (fixture `db`): la base real
del bot, `bottin_history.db`, no se toca.
"""
import bot


class TestAislamientoDeLaBase:
    def test_db_apunta_a_tmp(self, db):
        # Red de seguridad: si el monkeypatch de DB_NAME se cae, esto falla
        # antes de que cualquier test escriba en la base real del bot.
        assert bot.DB_NAME == str(db)
        assert "bottin_test.db" in bot.DB_NAME


class TestGuardarPartidos:
    def test_guarda_y_devuelve_la_cantidad(self, db):
        filas = [
            ("River Plate", "Boca", "G", 2, 1, "2026-05-10"),
            ("River Plate", "Vélez", "E", 1, 1, "2026-05-17"),
        ]
        assert bot._guardar_partidos(filas) == 2

    def test_lista_vacia_no_hace_nada(self, db):
        assert bot._guardar_partidos([]) == 0

    def test_el_mismo_partido_no_se_guarda_dos_veces(self, db):
        # ESPN devuelve el partido tanto en el scoreboard como en el calendario.
        fila = [("River Plate", "Boca", "G", 2, 1, "2026-05-10")]
        assert bot._guardar_partidos(fila) == 1
        assert bot._guardar_partidos(fila) == 0


class TestConsultarHistorial:
    def test_sin_registro_devuelve_none(self, db):
        assert bot._consultar_historial("river") == (None, [])

    def test_encuentra_el_equipo(self, db):
        bot._guardar_partidos([
            ("River Plate", "Boca", "G", 2, 1, "2026-05-10"),
        ])
        equipo, filas = bot._consultar_historial("river")
        assert equipo == "River Plate"
        assert len(filas) == 1

    def test_busca_sin_distincion_de_tildes(self, db):
        # Regresión del bug: el patrón se normaliza a "velez" pero la base
        # guarda "Vélez Sarsfield"; con LIKE eso no coincidía nunca.
        bot._guardar_partidos([
            ("Vélez Sarsfield", "River", "P", 0, 2, "2026-05-10"),
        ])
        equipo, _ = bot._consultar_historial("velez")
        assert equipo == "Vélez Sarsfield"

    def test_ordenado_de_lo_mas_reciente_a_lo_mas_viejo(self, db):
        bot._guardar_partidos([
            ("River Plate", "Boca", "G", 2, 1, "2026-05-10"),
            ("River Plate", "Vélez", "E", 1, 1, "2026-05-17"),
        ])
        _, filas = bot._consultar_historial("river")
        fechas = [fila[5] for fila in filas]
        assert fechas == sorted(fechas, reverse=True)

    def test_con_varios_equipos_elige_el_que_mas_tiene(self, db):
        # Dos equipos cuyo nombre contiene el patrón: gana el de más partidos.
        bot._guardar_partidos([
            ("River Plate", "Boca", "G", 2, 1, "2026-05-10"),
            ("River", "Boca", "E", 1, 1, "2026-05-11"),
            ("River", "Vélez", "G", 3, 0, "2026-05-12"),
        ])
        equipo, filas = bot._consultar_historial("river")
        assert equipo == "River"
        assert len(filas) == 2

    def test_consulta_vacia_devuelve_none(self, db):
        assert bot._consultar_historial("  ") == (None, [])


class TestEquipoPendienteDeCargar:
    def test_sin_registro_esta_pendiente(self, db):
        assert bot._equipo_pendiente_de_cargar("boca") is True

    def test_tras_marcar_no_esta_pendiente(self, db):
        bot._marcar_equipo_cargado("boca")
        assert bot._equipo_pendiente_de_cargar("boca") is False

    def test_marcar_es_idempotente(self, db):
        bot._marcar_equipo_cargado("boca")
        bot._marcar_equipo_cargado("boca")
        assert bot._equipo_pendiente_de_cargar("boca") is False
