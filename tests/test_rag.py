"""RAG sobre los PDFs reales de `docs/`.

Son los tests más valiosos de la suite: replican la batería que se verificó
a mano y protegen los dos arreglos delicados del ranking (el mapa de
sinónimos "fundo → fundado/fundacion" y el bonus por club).
"""
import bot


def _primera_fuente(contexto: str) -> str:
    """Fuente del fragmento mejor puntuado (el primero que lista el contexto)."""
    return contexto.split("[Fuente:")[1].split("]")[0]


class TestIndiceDeDocumentos:
    def test_ocho_documentos_cargados(self):
        # Regresión: si borran un PDF de docs/, esto falla enseguida.
        archivos = {archivo for archivo, *_ in bot._documentos_cargados()}
        assert len(archivos) == 8, f"solo hay {len(archivos)}: {sorted(archivos)}"

    def test_reglamento_afa_presente(self):
        archivos = {archivo for archivo, *_ in bot._documentos_cargados()}
        assert "reglameto_afa.pdf" in archivos

    def test_tiene_paginas(self):
        assert len(bot._documentos_cargados()) > 50


class TestTokensClub:
    def test_historia_de_clube_saca_genericos(self):
        # "historia_club_san_lorenzo.pdf" → {"lorenzo"}: "san" tiene 3 letras
        # y "historia/club/pdf" son genéricos.
        assert bot._tokens_club("historia_club_san_lorenzo.pdf") == {"lorenzo"}

    def test_historia_de_boca_incluye_ambas_palabras(self):
        assert bot._tokens_club("historia_club_boca_juniors.pdf") == {"boca", "juniors"}

    def test_archivo_de_historia_de_river(self):
        assert bot._tokens_club("historia_club_river_plate.pdf") == {"river", "plate"}

    def test_archivos_de_historia_no_dan_bonus(self):
        # Solo historia_club_* pueden llevar bonus: "formato" está en el nombre
        # del PDF de la Libertadores y no debe ganarle al reglamento.
        assert bot._tokens_club("formato_copa_libertadores.pdf") == set()
        assert bot._tokens_club("reglameto_afa.pdf") == set()


class TestTrocear:
    def test_no_pierde_contenido(self):
        texto = "Primer párrafo.\n\nSegundo párrafo con más texto.\n\nTercero."
        unido = " ".join(bot._trocear(texto))
        for parte in ("Primer", "Segundo", "Tercero"):
            assert parte in unido

    def test_respeta_el_tamanio_maximo(self):
        texto = "\n\n".join(f"Oración número {i} con un poco de texto." for i in range(50))
        for trozo in bot._trocear(texto, tamanio=200):
            assert len(trozo) <= 200 + 50  # holgura por el corte por espacios

    def test_parrafo_largo_se_parte(self):
        texto = "palabra " * 500  # un solo párrafo de ~4000 caracteres
        trozos = bot._trocear(texto, tamanio=300)
        assert len(trozos) > 1
        assert all(len(t) <= 350 for t in trozos)


class TestFundacionDeClubes:
    """Cada club debe salir con su propio PDF de historia, no con el de otro."""

    CONSULTAS = [
        ("¿Cuándo se fundó Boca Juniors?", "historia_club_boca_juniors.pdf"),
        ("¿Cuándo se fundó River Plate?", "historia_club_river_plate.pdf"),
        ("¿Cuándo se fundó San Lorenzo?", "historia_club_san_lorenzo.pdf"),
        ("¿Cuándo se fundó Independiente?", "historia_club_independiente.pdf"),
        ("¿Cuándo se fundó Estudiantes?", "historia_club_estudiantes.pdf"),
        ("¿Cuándo se fundó Racing?", "historia_club_racing.pdf"),
    ]

    def test_fundacion_boca(self):
        self._verificar(*self.CONSULTAS[0])

    def test_fundacion_river(self):
        self._verificar(*self.CONSULTAS[1])

    def test_fundacion_san_lorenzo(self):
        self._verificar(*self.CONSULTAS[2])

    def test_fundacion_independiente(self):
        self._verificar(*self.CONSULTAS[3])

    def test_fundacion_estudiantes(self):
        self._verificar(*self.CONSULTAS[4])

    def test_fundacion_racing(self):
        # Regresión: antes "¿cuándo se fundó Racing?" traía primero una página
        # de Estudiantes, que cita a Racing como rival.
        self._verificar(*self.CONSULTAS[5])

    @staticmethod
    def _verificar(consulta: str, archivo_esperado: str):
        contexto = bot.buscar_en_pdfs(consulta)
        assert archivo_esperado in contexto, (
            f"{consulta!r} no trajo {archivo_esperado}\nFuentes: "
            f"{contexto.count('[Fuente:')} fragmentos")
        # Y que hable de fundación, no de cualquier otra parte de la historia.
        assert "fund" in contexto.lower()


class TestHistoriaDeClubes:
    def test_historia_de_river(self):
        contexto = bot.buscar_en_pdfs("¿Cuál es la historia de River Plate?")
        assert "historia_club_river_plate.pdf" in contexto

    def test_historia_de_san_lorenzo(self):
        contexto = bot.buscar_en_pdfs("¿Cuál es la historia de San Lorenzo?")
        assert "historia_club_san_lorenzo.pdf" in contexto


class TestReglamentoYFormato:
    def test_formato_del_torneo_va_al_reglamento(self):
        # El caso 6 de casos.json: "formato" está en el nombre del PDF de la
        # Libertadores, pero la respuesta correcta es el reglamento de la AFA.
        contexto = bot.buscar_en_pdfs(
            "¿Cuántos equipos tiene y cómo es el formato del torneo?")
        assert "reglameto_afa.pdf" in _primera_fuente(contexto), (
            f"ganó {_primera_fuente(contexto)!r}")

    def test_duracion_del_partido_menciona_los_90_minutos(self):
        # Caso 7 de casos.json.
        contexto = bot.buscar_en_pdfs(
            "¿Qué dice el reglamento sobre la duración del partido?")
        assert "reglameto_afa.pdf" in contexto
        assert "90" in contexto

    def test_copa_libertadores_trae_su_pdf(self):
        # Caso 8 de casos.json.
        contexto = bot.buscar_en_pdfs("¿Cómo se juega la Copa Libertadores?")
        assert "formato_copa_libertadores.pdf" in contexto


class TestConsultasSinResultado:
    def test_consulta_sin_claves_devuelve_vacio(self):
        assert bot.buscar_en_pdfs("¿Y la?") == ""

    def test_consulta_vacia_devuelve_vacio(self):
        assert bot.buscar_en_pdfs("") == ""
