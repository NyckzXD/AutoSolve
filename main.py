import hashlib
import logging
import os
import queue
import re
import socket
import sys
import threading
import time

from dotenv import load_dotenv
from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchWindowException,
    StaleElementReferenceException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
import openai

load_dotenv()  # antes das constantes abaixo, que podem ler variáveis do .env

POLL_SECONDS = 2
MIN_TAMANHO_PERGUNTA = 10
CONFIRMACAO_DELAY = 0.5  # tempo entre duas leituras, para não processar a página no meio de um re-render
DEBUGGER_ADDRESS = "127.0.0.1:9222"

# Groq tem um free tier permanente e é compatível com a API da OpenAI:
# só muda a chave, a base_url e o nome do modelo.
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
API_TIMEOUT_SECONDS = 30
API_MAX_RETRIES = 2

SYSTEM_PROMPT = (
    "Você resolve questões de múltipla escolha. Responda somente com a letra da "
    "alternativa correta (por exemplo: B), sem explicações."
)

log = logging.getLogger("quiz_assistant")


# ---------------------------------------------------------------------------
# Funções puras (sem Selenium/Tkinter/rede): fáceis de testar
# ---------------------------------------------------------------------------

def assinatura(pergunta, alternativas):
    bruto = pergunta + "|" + "|".join(alternativas)
    return hashlib.sha256(bruto.encode("utf-8")).hexdigest()


def montar_prompt(pergunta, alternativas):
    texto_alternativas = "\n".join(
        f"{chr(65 + i)}) {texto}" for i, texto in enumerate(alternativas)
    )
    return (
        f"Pergunta: {pergunta}\n\nAlternativas:\n{texto_alternativas}\n\n"
        "Responda apenas com a letra correta."
    )


_RE_LETRA_ISOLADA = re.compile(r"^\W*([A-Za-z])\W*$")
# Palavra-chave sem diferenciar maiúsculas, mas a letra tem de vir em maiúscula
# (evita confundir o artigo "a" de "a resposta é a casa" com a alternativa A).
_RE_LETRA_COM_CONTEXTO = re.compile(
    r"(?i:letra|alternativa|op[çc][ãa]o|resposta(?:\s+correta)?)"
    r"(?:\s+(?i:é|e|correta\s+é))?\s*[:\-]?\s*\W*([A-Z])(?![A-Za-zÀ-ÿ])"
)


def extrair_letra(resposta, total_alternativas):
    """Extrai a letra escolhida da resposta da IA; devolve None se não der para ter certeza."""
    if not resposta:
        return None
    texto = resposta.strip()

    candidata = None
    m = _RE_LETRA_ISOLADA.match(texto)
    if m:
        candidata = m.group(1).upper()
    else:
        m = _RE_LETRA_COM_CONTEXTO.search(texto)
        if m:
            candidata = m.group(1)

    if candidata is None:
        return None
    indice = ord(candidata) - 65
    return candidata if 0 <= indice < total_alternativas else None


def formatar_resposta(resposta_ia, alternativas):
    """Texto exibido no popup: 'B) texto da alternativa' quando a letra é reconhecida,
    senão a resposta crua da IA."""
    letra = extrair_letra(resposta_ia, len(alternativas))
    if letra is None:
        return resposta_ia
    return f"{letra}) {alternativas[ord(letra) - 65]}"


# ---------------------------------------------------------------------------
# Popup (thread dedicada, para não bloquear o monitoramento)
# ---------------------------------------------------------------------------

class PopupWorker:
    """Exibe os popups numa única thread própria. O Tkinter exige que todas as chamadas
    de uma janela venham da mesma thread; uma fila serializa os pedidos."""

    def __init__(self):
        self._fila = queue.Queue()
        self._thread = threading.Thread(target=self._rodar, name="popup", daemon=True)
        self._thread.start()

    def mostrar(self, pergunta, resposta):
        self._fila.put((pergunta, resposta))

    def _rodar(self):
        import tkinter as tk
        from tkinter import messagebox

        while True:
            pergunta, resposta = self._fila.get()
            try:
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                # Só os primeiros 150 caracteres da pergunta, para não estourar o popup
                messagebox.showinfo(
                    "Resposta da IA", f"Questão: {pergunta[:150]}...\n\nSugerida: {resposta}"
                )
                root.destroy()
            except Exception:
                log.exception("Erro ao exibir o popup")


# ---------------------------------------------------------------------------
# Leitura da página (Moodle)
# ---------------------------------------------------------------------------

def buscar_alternativas(escopo):
    # No Moodle as alternativas ficam dentro de .answer ou .flex-fill
    elementos_opcoes = escopo.find_elements(By.CSS_SELECTOR, ".answer div.flex-fill.ml-1")
    if not elementos_opcoes:
        # Seletor alternativo caso o de cima mude
        elementos_opcoes = escopo.find_elements(By.CSS_SELECTOR, ".answer label")
    return [opt.text for opt in elementos_opcoes if opt.text.strip() != ""]


def localizar_container(bloco_qtext):
    """Sobe até a div.que (bloco Moodle de uma questão), para restringir a busca de
    alternativas a essa questão específica quando há várias na mesma página."""
    try:
        return bloco_qtext.find_element(
            By.XPATH,
            "./ancestor::div[contains(concat(' ', normalize-space(@class), ' '), ' que ')][1]",
        )
    except Exception:
        return None


def ler_questao(driver, bloco_qtext, permitir_fallback_global):
    pergunta = bloco_qtext.text
    if len(pergunta.strip()) <= MIN_TAMANHO_PERGUNTA:
        return None

    container = localizar_container(bloco_qtext)
    if container is not None:
        lista_alternativas = buscar_alternativas(container)
    elif permitir_fallback_global:
        # Tema sem div.que reconhecível: só é seguro buscar na página toda
        # quando há uma única questão nela (senão misturaria alternativas de outra).
        lista_alternativas = buscar_alternativas(driver)
    else:
        lista_alternativas = []

    if not lista_alternativas:
        return None

    return pergunta, lista_alternativas


# ---------------------------------------------------------------------------
# IA
# ---------------------------------------------------------------------------

def criar_cliente():
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return None
    return openai.OpenAI(
        api_key=api_key,
        base_url=GROQ_BASE_URL,
        timeout=API_TIMEOUT_SECONDS,
        max_retries=API_MAX_RETRIES,
    )


def consultar_ia(client, pergunta, alternativas):
    completion = client.chat.completions.create(
        model=GROQ_MODEL,
        temperature=0,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": montar_prompt(pergunta, alternativas)},
        ],
    )
    return (completion.choices[0].message.content or "").strip()


def processar_questao(client, popup, pergunta, alternativas):
    try:
        log.info("Solicitando resposta para a IA...")
        resposta_ia = consultar_ia(client, pergunta, alternativas)
        log.info("IA respondeu: %s", resposta_ia)
        popup.mostrar(pergunta, formatar_resposta(resposta_ia, alternativas))
    except Exception as e:
        log.error("Erro ao consultar a IA: %s", e)


# ---------------------------------------------------------------------------
# Loop principal
# ---------------------------------------------------------------------------

def chrome_disponivel(endereco=DEBUGGER_ADDRESS, timeout=2):
    """Testa se há algo escutando na porta de depuração. Sem isso, webdriver.Chrome()
    pode ficar muito tempo parado esperando o chromedriver quando o Chrome está fechado."""
    host, _, porta = endereco.rpartition(":")
    try:
        with socket.create_connection((host, int(porta)), timeout=timeout):
            return True
    except OSError:
        return False


def conectar_chrome():
    options = Options()
    options.add_experimental_option("debuggerAddress", DEBUGGER_ADDRESS)
    return webdriver.Chrome(options=options)


def varrer_abas(driver, client, popup, estado_perguntas):
    """Uma passada por todas as abas. Devolve False se a conexão com o Chrome foi perdida."""
    try:
        handles = driver.window_handles
    except WebDriverException as e:
        log.error("Perdi a conexão com o Chrome (fechou o navegador?): %s", e)
        return False

    for handle in handles:
        try:
            driver.switch_to.window(handle)
            blocos_qtext = driver.find_elements(By.CLASS_NAME, "qtext")
        except NoSuchWindowException:
            # Aba foi fechada entre o window_handles e o switch_to; ignora e segue.
            continue
        except WebDriverException as e:
            log.warning("Erro ao acessar aba: %s", e)
            continue

        permitir_fallback_global = len(blocos_qtext) == 1

        for indice, bloco in enumerate(blocos_qtext):
            try:
                resultado = ler_questao(driver, bloco, permitir_fallback_global)
                if resultado is None:
                    continue

                # Segunda leitura para confirmar que o DOM não está no meio de uma
                # transição (evita processar uma questão com texto truncado).
                time.sleep(CONFIRMACAO_DELAY)
                confirmacao = ler_questao(driver, bloco, permitir_fallback_global)
                if confirmacao is None or confirmacao != resultado:
                    continue

                pergunta, alternativas = confirmacao
                chave = (handle, indice)
                nova_assinatura = assinatura(pergunta, alternativas)

                if estado_perguntas.get(chave) != nova_assinatura:
                    estado_perguntas[chave] = nova_assinatura
                    log.info("--- Nova questão detectada (posição %d) ---", indice + 1)
                    processar_questao(client, popup, pergunta, alternativas)

            except StaleElementReferenceException:
                # Página re-renderizou entre o find_elements e a leitura; tenta de novo no próximo poll.
                continue
            except Exception as e:
                log.error("Erro ao processar questão: %s", e)
                continue

    return True


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    client = criar_cliente()
    if client is None:
        log.error("GROQ_API_KEY não definida. Crie um .env a partir de chave.env.example.")
        return 1

    if not chrome_disponivel():
        log.error(
            "Chrome não encontrado em %s. Inicie-o com: chrome --remote-debugging-port=9222",
            DEBUGGER_ADDRESS,
        )
        return 1

    try:
        driver = conectar_chrome()
    except Exception as e:
        log.error(
            "Erro ao conectar: %s. Verifique se o Chrome está aberto na porta 9222.", e
        )
        return 1
    log.info("Conectado ao Chrome com sucesso!")

    popup = PopupWorker()
    estado_perguntas = {}  # (janela, indice_na_pagina) -> assinatura da última questão processada nela

    log.info("Monitorando a página... Troque de questão no navegador para disparar.")
    log.info(
        "(poll a cada %ss; cobre todas as abas abertas e todas as questões de cada página)",
        POLL_SECONDS,
    )

    try:
        while varrer_abas(driver, client, popup, estado_perguntas):
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        log.info("Encerrado pelo usuário.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
