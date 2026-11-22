import pytest

import main


class TestExtrairLetra:
    @pytest.mark.parametrize(
        "resposta, esperado",
        [
            ("B", "B"),
            ("b", "B"),
            ("  C.  ", "C"),
            ("**D**", "D"),
            ("(A)", "A"),
            ("A resposta correta é B", "B"),
            ("Resposta: C", "C"),
            ("A alternativa correta é a letra D.", "D"),
            ("Letra B)", "B"),
        ],
    )
    def test_reconhece_letra(self, resposta, esperado):
        assert main.extrair_letra(resposta, 5) == esperado

    @pytest.mark.parametrize(
        "resposta",
        ["", "   ", None, "Não sei responder", "A resposta é a casa azul"],
    )
    def test_devolve_none_quando_ambiguo(self, resposta):
        assert main.extrair_letra(resposta, 5) is None

    def test_letra_fora_do_intervalo(self):
        assert main.extrair_letra("E", 4) is None
        assert main.extrair_letra("D", 4) == "D"


class TestFormatarResposta:
    def test_mapeia_letra_para_texto(self):
        alternativas = ["Paris", "Roma", "Lisboa"]
        assert main.formatar_resposta("B", alternativas) == "B) Roma"

    def test_mantem_resposta_crua_se_nao_reconhece(self):
        assert main.formatar_resposta("Não sei", ["x", "y"]) == "Não sei"


class TestMontarPrompt:
    def test_lista_alternativas_com_letras(self):
        prompt = main.montar_prompt("Capital da França?", ["Paris", "Roma"])
        assert "Pergunta: Capital da França?" in prompt
        assert "A) Paris" in prompt
        assert "B) Roma" in prompt
        assert prompt.endswith("Responda apenas com a letra correta.")


class TestAssinatura:
    def test_deterministica(self):
        assert main.assinatura("p", ["a", "b"]) == main.assinatura("p", ["a", "b"])

    def test_muda_com_pergunta_ou_alternativas(self):
        base = main.assinatura("p", ["a", "b"])
        assert main.assinatura("q", ["a", "b"]) != base
        assert main.assinatura("p", ["a", "c"]) != base
        assert main.assinatura("p", ["b", "a"]) != base


class TestChromeDisponivel:
    def test_porta_fechada(self):
        import socket

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            porta = s.getsockname()[1]
        # porta liberada logo acima: ninguém escutando
        assert main.chrome_disponivel(f"127.0.0.1:{porta}", timeout=1) is False

    def test_porta_aberta(self):
        import socket

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            porta = s.getsockname()[1]
            assert main.chrome_disponivel(f"127.0.0.1:{porta}", timeout=1) is True


class TestCriarCliente:
    def test_sem_chave_devolve_none(self, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        assert main.criar_cliente() is None

    def test_com_chave_cria_cliente(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "chave-de-teste")
        assert main.criar_cliente() is not None
