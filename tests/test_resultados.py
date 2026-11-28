"""Marcadores y eventos de ESPN: resultado del partido y parseo de goles."""
from bot import _evento_finalizado, _gole_del_competidor, _resultado_del_partido


class TestResultadoDelPartido:
    def test_victoria(self):
        assert _resultado_del_partido(2, 1) == "G"

    def test_empate(self):
        assert _resultado_del_partido(1, 1) == "E"

    def test_derrota(self):
        assert _resultado_del_partido(0, 3) == "P"

    def test_gol_en_contra_nomas(self):
        assert _resultado_del_partido(1, 2) == "P"


class TestGoleDelCompetidor:
    def test_score_entero(self):
        assert _gole_del_competidor({"score": 2}) == 2

    def test_score_como_dict_con_value(self):
        # Algunos eventos de ESPN traen el marcador anidado.
        assert _gole_del_competidor({"score": {"value": 3}}) == 3

    def test_score_decimal_se_redondea_hacia_abajo(self):
        assert _gole_del_competidor({"score": "1.0"}) == 1

    def test_score_ausente(self):
        assert _gole_del_competidor({}) is None

    def test_score_no_numerico(self):
        assert _gole_del_competidor({"score": "abc"}) is None


def _evento_boca_river(estado: dict, fecha: str = "2026-05-10T20:00:00Z") -> dict:
    """Evento de ESPN mínimo con local Boca y visita River."""
    return {
        "date": fecha,
        "competitions": [{
            "status": {"type": estado},
            "competitors": [
                {"homeAway": "home", "team": {"displayName": "Boca"},
                 "score": 2},
                {"homeAway": "away", "team": {"displayName": "River"},
                 "score": 1},
            ],
        }],
    }


class TestEventoFinalizado:
    def test_partido_terminado(self):
        fin = _evento_finalizado(_evento_boca_river(
            {"state": "post", "name": "STATUS_FULL_TIME"}))
        assert fin == ("2026-05-10", "Boca", 2, "River", 1)

    def test_partido_en_vivo_devuelve_none(self):
        assert _evento_finalizado(
            _evento_boca_river({"state": "in", "name": "STATUS_IN_PROGRESS"})) is None

    def test_sin_fecha_devuelve_none(self):
        evento = _evento_boca_river({"state": "post"}, fecha="")
        assert _evento_finalizado(evento) is None

    def test_sin_competidores_devuelve_none(self):
        evento = {"date": "2026-05-10", "competitions": [{"status": {"type": {"state": "post"}}}]}
        assert _evento_finalizado(evento) is None

    def test_evento_vacio_devuelve_none(self):
        assert _evento_finalizado({}) is None
