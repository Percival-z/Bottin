"""Normalización de texto: tildes, puntuación y extracción de equipos.

Es la base de todo (RAG, historial, scraping): si esto se rompe, el bot no
encuentra nada aunque los datos estén bien.
"""
from bot import _extraer_equipo, _normalizar_nombre, _sin_acentos


class TestSinAcentos:
    def test_quita_tildes(self):
        assert _sin_acentos("fútbol") == "futbol"

    def test_conserva_letras_ascii(self):
        assert _sin_acentos("Boca Juniors") == "Boca Juniors"

    def test_enye(self):
        assert _sin_acentos("menú") == "menu"


class TestNormalizarNombre:
    def test_minusculas_sin_tildes(self):
        # El caso real: "¿Cuándo se fundó River?" → "fundo" (no "fundado"),
        # por eso el RAG necesita el mapa de sinónimos.
        assert _normalizar_nombre("¿Cuándo se fundó River?") == "cuando se fundo river"

    def test_quita_puntuacion(self):
        assert _normalizar_nombre("¡Boca, River!") == "boca river"

    def test_espacios_simples(self):
        assert _normalizar_nombre("  San   Lorenzo  ") == "san lorenzo"

    def test_guiones_bajos_se_vuelven_espacio(self):
        # Los nombres de archivo vienen con guiones bajos.
        assert _normalizar_nombre("historia_club_san_lorenzo") == "historia club san lorenzo"

    def test_texto_vacio(self):
        assert _normalizar_nombre("") == ""


class TestExtraerEquipo:
    def test_extrae_el_ultimo_equipo(self):
        texto = "Dame las estadísticas de los últimos partidos de Boca."
        assert _extraer_equipo(texto) == "Boca"

    def test_ignora_palabras_de_relleno(self):
        assert _extraer_equipo("¿Cómo viene River?") == "River"

    def test_palabras_cortas_no_cuentan(self):
        # "de", "los", "de" son stopwords o muy cortas: se queda con "Liga".
        assert _extraer_equipo("tabla de la Liga") == "Liga"

    def test_sin_candidatos_devuelve_vacio(self):
        assert _extraer_equipo("") == ""
        assert _extraer_equipo("de la") == ""

    def test_nombres_compuestos_con_guion(self):
        assert _extraer_equipo("racha de Vélez-Sarsfield") == "Vélez-Sarsfield"
