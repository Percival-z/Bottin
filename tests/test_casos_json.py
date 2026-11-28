"""`casos.json`: la lista de 16 casos de evaluación.

Estos tests no ejecutan el bot entero (eso necesita Telegram y Gemini):
verifican que la lista esté bien formada y que los casos de RAG se cumplan
contra los PDFs reales, que es lo que se puede comprobar sin red.
"""
import json
from pathlib import Path

import bot

RAIZ = Path(__file__).resolve().parent.parent
CASOS = json.loads((RAIZ / "casos.json").read_text(encoding="utf-8"))


def _caso(n: int) -> dict:
    return next(c for c in CASOS["casos"] if c["n"] == n)


class TestEstructuraDelArchivo:
    def test_total_consistente(self):
        assert CASOS["total"] == 16
        assert len(CASOS["casos"]) == 16

    def test_numeros_correlativos_y_unicos(self):
        numeros = [c["n"] for c in CASOS["casos"]]
        assert numeros == list(range(1, 17))

    def test_campos_obligatorios_en_todos(self):
        for caso in CASOS["casos"]:
            for campo in ("n", "categoria", "entrada", "tipo", "demuestra", "esperado"):
                assert campo in caso, f"el caso {caso.get('n')} no tiene '{campo}'"

    def test_tipos_validos(self):
        for caso in CASOS["casos"]:
            assert caso["tipo"] in ("texto", "foto", "audio"), caso

    def test_los_multimodales_son_foto_o_audio(self):
        # Los casos que se prueban con material multimedia van como foto/audio.
        for caso in CASOS["casos"]:
            if caso["categoria"].startswith("Multimodalidad"):
                assert caso["tipo"] in ("foto", "audio"), caso

    def test_el_manual_de_texto_es_el_borrado(self):
        # /olvidar es "manual" no por multimodalidad: borra la base, así que
        # hay que ejectarlo a mano y al final de la tanda.
        caso16 = _caso(16)
        assert caso16["manual"] is True
        assert caso16["tipo"] == "texto"

    def test_el_caso_11_depende_del_10(self):
        # La memoria secuencial: "¿De qué equipo soy hincha?" necesita que
        # antes se haya enviado "Soy hincha de River".
        assert _caso(11)["requiere"] == 10


class TestCasosDeRAG:
    """Los 3 casos de RAG de la lista, ejecutados contra los PDFs reales."""

    def test_caso_6_formato_del_torneo(self):
        contexto = bot.buscar_en_pdfs(_caso(6)["entrada"])
        primera = contexto.split("[Fuente:")[1].split("]")[0]
        assert "reglameto_afa.pdf" in primera, f"ganó {primera!r}"

    def test_caso_7_duracion_del_partido(self):
        contexto = bot.buscar_en_pdfs(_caso(7)["entrada"])
        assert "reglameto_afa.pdf" in contexto
        assert "90" in contexto

    def test_caso_8_copa_libertadores(self):
        contexto = bot.buscar_en_pdfs(_caso(8)["entrada"])
        assert "formato_copa_libertadores.pdf" in contexto


class TestCasosQueRequierenRed:
    """Los casos que no se pueden verificar sin Telegram/Gemini quedan
    identificados acá: si alguno se vuelve local, conviene moverlo."""

    def test_casos_manuales_listados(self):
        manuales = [c["n"] for c in CASOS["casos"] if c.get("manual")]
        assert manuales == [12, 13, 16]

    def test_casos_de_comando(self):
        comandos = [c["entrada"] for c in CASOS["casos"]
                    if c["entrada"].startswith("/")]
        assert comandos == ["/start", "/help", "/olvidar"]
