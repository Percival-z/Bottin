"""Salida estructurada de Gemini validada con Pydantic.

El bot solo guarda/anuncia el análisis si el modelo devuelve algo que
cumple el esquema: si Pydantic rechaza, no se toca la base.
"""
import pytest
from pydantic import ValidationError

from bot import AnalisisPartido


def _analisis_valido(**cambios) -> dict:
    datos = {
        "equipo_local": "Boca",
        "equipo_visita": "River",
        "prediccion": "gana_local",
        "clave": "La localía del Monumental",
        "confianza": 70,
    }
    datos.update(cambios)
    return datos


class TestAnalisisPartido:
    def test_analisis_valido(self):
        a = AnalisisPartido(**_analisis_valido())
        assert a.equipo_local == "Boca"
        assert a.prediccion == "gana_local"
        assert a.confianza == 70

    def test_prediccion_acepta_las_tres_opciones(self):
        for prediccion in ("gana_local", "empate", "gana_visita"):
            a = AnalisisPartido(**_analisis_valido(prediccion=prediccion))
            assert a.prediccion == prediccion

    def test_prediccion_invalida_se_rechaza(self):
        with pytest.raises(ValidationError):
            AnalisisPartido(**_analisis_valido(prediccion="gana_boca"))

    @pytest.mark.parametrize("confianza", [-1, 101])
    def test_confianza_fuera_de_rango_se_rechaza(self, confianza):
        with pytest.raises(ValidationError):
            AnalisisPartido(**_analisis_valido(confianza=confianza))

    def test_confianza_en_los_limites_es_valida(self):
        for confianza in (0, 100):
            assert AnalisisPartido(**_analisis_valido(confianza=confianza)).confianza == confianza

    def test_faltan_campos_obligatorios(self):
        with pytest.raises(ValidationError):
            AnalisisPartido(equipo_local="Boca")  # type: ignore[call-arg]
