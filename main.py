import hashlib
import logging
import os
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

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

BASE_DIR = Path(__file__).resolve().parent

# Antes das constantes abaixo, que leem variáveis do .env. Aceita .env ou key.env.
load_dotenv(BASE_DIR / ".env")
load_dotenv(BASE_DIR / "key.env")


def _env_float(nome, padrao):
    try:
        return float(os.environ.get(nome, padrao))
    except ValueError:
        return padrao


def _env_int(nome, padrao):
    try:
        return int(os.environ.get(nome, padrao))
    except ValueError:
        return padrao


POLL_SECONDS = _env_float("POLL_SECONDS", 2)
MIN_TAMANHO_PERGUNTA = 10
CONFIRMACAO_DELAY = 0.5  # tempo entre duas leituras, para não processar a página no meio de um re-render

# Chrome
CHROME_PORT = _env_int("CHROME_PORT", 9222)
DEBUGGER_ADDRESS = f"127.0.0.1:{CHROME_PORT}"
CHROME_PATH = os.environ.get("CHROME_PATH") or None
# Desde o Chrome 136, --remote-debugging-port é ignorado no perfil padrão: é obrigatório
# usar um --user-data-dir próprio. Este perfil fica salvo, então o login do Moodle persiste.
CHROME_PROFILE_DIR = Path(os.environ.get("CHROME_PROFILE_DIR") or BASE_DIR / "perfil-chrome")
URL_INICIAL = os.environ.get("URL_INICIAL") or None
CHROME_START_TIMEOUT = _env_float("CHROME_START_TIMEOUT", 20)

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


def resumir(texto, limite):
    """Junta espaços/quebras de linha e corta em `limite` caracteres, com reticências."""
    texto = " ".join(texto.split())
    return texto if len(texto) <= limite else texto[: limite - 1].rstrip() + "…"


def formatar_resposta(resposta_ia, alternativas):
    """Texto exibido na janela: 'B) texto da alternativa' quando a letra é reconhecida,
    senão a resposta crua da IA."""
    letra = extrair_letra(resposta_ia, len(alternativas))
    if letra is None:
        return resposta_ia
    return f"{letra}) {alternativas[ord(letra) - 65]}"


# ---------------------------------------------------------------------------
# Chrome: localizar, abrir e esperar a porta de depuração
# ---------------------------------------------------------------------------

NOMES_NO_PATH = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")


def candidatos_chrome(sistema, env):
    """Caminhos de instalação padrão do Chrome para cada sistema operacional."""
    if sistema.startswith("win"):
        bases = [env.get("PROGRAMFILES"), env.get("PROGRAMFILES(X86)"), env.get("LOCALAPPDATA")]
        return [str(Path(b) / "Google" / "Chrome" / "Application" / "chrome.exe") for b in bases if b]
    if sistema == "darwin":
        app = "Google Chrome.app/Contents/MacOS/Google Chrome"
        return [f"/Applications/{app}", str(Path.home() / "Applications" / app)]
    return []  # Linux: procura pelo PATH


def encontrar_chrome(caminho_configurado, sistema=None, env=None,
                     existe=os.path.isfile, which=shutil.which):
    """Devolve o executável do Chrome, ou None. CHROME_PATH no .env tem prioridade."""
    if caminho_configurado:
        return caminho_configurado if existe(caminho_configurado) else None

    sistema = sistema or sys.platform
    env = os.environ if env is None else env
    for caminho in candidatos_chrome(sistema, env):
        if existe(caminho):
            return caminho
    for nome in NOMES_NO_PATH:
        achado = which(nome)
        if achado:
            return achado
    return None


def montar_comando_chrome(executavel, porta, perfil, url=None):
    comando = [
        executavel,
        f"--remote-debugging-port={porta}",
        f"--user-data-dir={perfil}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if url:
        comando.append(url)
    return comando


def iniciar_chrome(executavel, porta, perfil, url=None):
    """Abre o Chrome desacoplado do script: ele continua aberto depois do Ctrl+C,
    e na próxima execução o script só se reconecta."""
    Path(perfil).mkdir(parents=True, exist_ok=True)
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(montar_comando_chrome(executavel, porta, perfil, url), **kwargs)


def chrome_disponivel(endereco=DEBUGGER_ADDRESS, timeout=2):
    """Testa se há algo escutando na porta de depuração. Sem isso, webdriver.Chrome()
    pode ficar muito tempo parado esperando o chromedriver quando o Chrome está fechado."""
    host, _, porta = endereco.rpartition(":")
    try:
        with socket.create_connection((host, int(porta)), timeout=timeout):
            return True
    except OSError:
        return False


def aguardar_porta(endereco, timeout, intervalo=0.5):
    limite = time.monotonic() + timeout
    while time.monotonic() < limite:
        if chrome_disponivel(endereco, timeout=intervalo):
            return True
        time.sleep(intervalo)
    return chrome_disponivel(endereco, timeout=intervalo)


def garantir_chrome():
    """Reaproveita um Chrome já aberto na porta; senão, abre um com o perfil do projeto."""
    if chrome_disponivel():
        log.info("Chrome já aberto em %s; reaproveitando.", DEBUGGER_ADDRESS)
        return True

    executavel = encontrar_chrome(CHROME_PATH)
    if executavel is None:
        log.error(
            "Não encontrei o Chrome instalado. Defina CHROME_PATH no .env com o caminho "
            "do executável (ex.: C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe)."
        )
        return False

    log.info("Abrindo o Chrome (perfil em %s)...", CHROME_PROFILE_DIR)
    try:
        iniciar_chrome(executavel, CHROME_PORT, CHROME_PROFILE_DIR, URL_INICIAL)
    except OSError as e:
        log.error("Falha ao abrir o Chrome (%s): %s", executavel, e)
        return False

    if aguardar_porta(DEBUGGER_ADDRESS, CHROME_START_TIMEOUT):
        return True

    log.error(
        "O Chrome abriu, mas a porta %d não respondeu em %.0fs. Se já existe uma janela "
        "do Chrome usando o perfil %s sem a porta de depuração, feche-a e rode de novo.",
        CHROME_PORT, CHROME_START_TIMEOUT, CHROME_PROFILE_DIR,
    )
    return False


def conectar_chrome():
    options = Options()
    options.add_experimental_option("debuggerAddress", DEBUGGER_ADDRESS)
    return webdriver.Chrome(options=options)


# ---------------------------------------------------------------------------
# Saída das respostas: janela fixa (Tkinter) ou, sem interface gráfica, o terminal
# ---------------------------------------------------------------------------
# As duas classes têm a mesma interface: consultando(), mostrar() e erro(). Podem ser
# chamadas de qualquer thread.

class JanelaResposta:
    """Uma única janela pequena, sempre no topo, atualizada a cada questão (em vez de um
    popup modal por resposta). Roda na thread principal, porque o Tk no macOS exige isso;
    as outras threads só colocam pedidos numa fila, lida aqui a cada 100 ms."""

    INTERVALO_FILA_MS = 100
    LIMITE_PERGUNTA = 280

    def __init__(self, parar):
        import tkinter as tk

        self._fila = queue.Queue()
        self._parar = parar
        self._resposta_atual = ""

        self.root = tk.Tk()
        self.root.title("AutoSolve")
        self.root.geometry("400x230")
        self.root.minsize(280, 180)
        self.root.attributes("-topmost", True)
        self.root.protocol("WM_DELETE_WINDOW", self._parar.set)

        corpo = tk.Frame(self.root, padx=12, pady=10)
        corpo.pack(fill="both", expand=True)

        self.lbl_status = tk.Label(corpo, text="Aguardando questão...", fg="#666666", anchor="w")
        self.lbl_status.pack(fill="x")

        self.lbl_resposta = tk.Label(
            corpo, text="—", font=("Segoe UI", 16, "bold"), anchor="w", justify="left"
        )
        self.lbl_resposta.pack(fill="x", pady=(6, 6))

        self.lbl_pergunta = tk.Label(corpo, text="", fg="#444444", anchor="nw", justify="left")
        self.lbl_pergunta.pack(fill="both", expand=True)

        rodape = tk.Frame(corpo)
        rodape.pack(fill="x", side="bottom")
        self._topo = tk.BooleanVar(value=True)
        tk.Checkbutton(
            rodape, text="Sempre no topo", variable=self._topo, command=self._alternar_topo
        ).pack(side="left")
        self.btn_copiar = tk.Button(
            rodape, text="Copiar resposta", command=self._copiar, state="disabled"
        )
        self.btn_copiar.pack(side="right")

        # O texto quebra de acordo com a largura atual da janela.
        self.root.bind("<Configure>", self._ajustar_quebra)
        self.root.after(self.INTERVALO_FILA_MS, self._processar_fila)

    # --- chamados de qualquer thread -------------------------------------------------

    def consultando(self, pergunta):
        self._fila.put(("consultando", pergunta, None))

    def mostrar(self, pergunta, resposta):
        self._fila.put(("resposta", pergunta, resposta))

    def erro(self, pergunta, mensagem):
        self._fila.put(("erro", pergunta, mensagem))

    # --- só na thread do Tk ----------------------------------------------------------

    def rodar(self):
        self.root.mainloop()

    def _processar_fila(self):
        if self._parar.is_set():
            self.root.destroy()
            return
        try:
            while True:
                self._aplicar(*self._fila.get_nowait())
        except queue.Empty:
            pass
        self.root.after(self.INTERVALO_FILA_MS, self._processar_fila)

    def _aplicar(self, tipo, pergunta, texto):
        self.lbl_pergunta.config(text=resumir(pergunta, self.LIMITE_PERGUNTA))
        if tipo == "consultando":
            self.lbl_status.config(text="Consultando a IA...", fg="#666666")
            self.lbl_resposta.config(text="…", fg="#666666")
            self.btn_copiar.config(state="disabled")
            return

        hora = time.strftime("%H:%M:%S")
        if tipo == "erro":
            self.lbl_status.config(text=f"Erro às {hora}", fg="#b00020")
            self.lbl_resposta.config(text=resumir(texto, 120), fg="#b00020")
            self.btn_copiar.config(state="disabled")
            return

        self._resposta_atual = texto
        self.lbl_status.config(text=f"Respondida às {hora}", fg="#666666")
        self.lbl_resposta.config(text=texto, fg="#1b5e20")
        self.btn_copiar.config(state="normal")
        # Se a janela estava minimizada, volta a aparecer.
        if self.root.state() == "iconic":
            self.root.deiconify()

    def _alternar_topo(self):
        self.root.attributes("-topmost", self._topo.get())

    def _copiar(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self._resposta_atual)
        self.lbl_status.config(text="Resposta copiada.")

    def _ajustar_quebra(self, evento):
        if evento.widget is self.root:
            largura = max(evento.width - 30, 100)
            self.lbl_resposta.config(wraplength=largura)
            self.lbl_pergunta.config(wraplength=largura)


class SaidaTerminal:
    """Usada quando não há interface gráfica: a resposta só aparece no log."""

    def consultando(self, pergunta):
        pass  # o IAWorker já registra no log

    def mostrar(self, pergunta, resposta):
        log.info(">>> Resposta sugerida: %s", resposta)

    def erro(self, pergunta, mensagem):
        pass  # o IAWorker já registra no log


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


class IAWorker:
    """Consulta a IA numa thread própria, para a varredura das abas não travar esperando
    a API. Guarda as respostas por assinatura: a mesma questão vista de novo (em outra aba,
    ou depois de voltar a ela) não gera outra chamada."""

    def __init__(self, client, saida, consultar=consultar_ia):
        self._client = client
        self._saida = saida
        self._consultar = consultar
        self._fila = queue.Queue()
        self._cache = {}          # assinatura -> resposta formatada
        self._pendentes = set()   # assinaturas na fila ou sendo consultadas
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._rodar, name="ia", daemon=True)
        self._thread.start()

    def enviar(self, pergunta, alternativas):
        sig = assinatura(pergunta, alternativas)
        with self._lock:
            if sig in self._pendentes:
                return
            resposta = self._cache.get(sig)
            if resposta is None:
                self._pendentes.add(sig)
                self._fila.put((sig, pergunta, alternativas))
                return
        log.info("Questão já respondida antes; usando a resposta guardada.")
        self._saida.mostrar(pergunta, resposta)

    def aguardar(self):
        """Bloqueia até a fila esvaziar (usado nos testes)."""
        self._fila.join()

    def _rodar(self):
        while True:
            sig, pergunta, alternativas = self._fila.get()
            try:
                self._processar(sig, pergunta, alternativas)
            except Exception:
                # Nunca deixa a thread morrer: senão as próximas questões ficariam na fila para sempre.
                log.exception("Erro inesperado no worker da IA")
            finally:
                with self._lock:
                    self._pendentes.discard(sig)
                self._fila.task_done()

    def _processar(self, sig, pergunta, alternativas):
        self._saida.consultando(pergunta)
        try:
            log.info("Solicitando resposta para a IA...")
            resposta_ia = self._consultar(self._client, pergunta, alternativas)
            log.info("IA respondeu: %s", resposta_ia)
        except Exception as e:
            # Não vai para o cache: se a questão aparecer de novo, tenta outra vez.
            log.error("Erro ao consultar a IA: %s", e)
            self._saida.erro(pergunta, f"Erro ao consultar a IA: {e}")
            return

        resposta = formatar_resposta(resposta_ia, alternativas)
        with self._lock:
            self._cache[sig] = resposta
        self._saida.mostrar(pergunta, resposta)


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
# Loop principal
# ---------------------------------------------------------------------------

def varrer_abas(driver, ia, estado_perguntas):
    """Uma passada por todas as abas. Devolve False se a conexão com o Chrome foi perdida."""
    try:
        handles = driver.window_handles
    except WebDriverException as e:
        log.error("Perdi a conexão com o Chrome (fechou o navegador?): %s", e)
        return False

    # Esquece as abas que foram fechadas.
    abertas = set(handles)
    for chave in [c for c in estado_perguntas if c[0] not in abertas]:
        del estado_perguntas[chave]

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

                chave = (handle, indice)
                nova_assinatura = assinatura(*resultado)
                if estado_perguntas.get(chave) == nova_assinatura:
                    continue  # já tratada: nada de segunda leitura nem espera

                # Segunda leitura só para questões novas, para confirmar que o DOM não está
                # no meio de uma transição (evita processar uma questão com texto truncado).
                time.sleep(CONFIRMACAO_DELAY)
                if ler_questao(driver, bloco, permitir_fallback_global) != resultado:
                    continue

                estado_perguntas[chave] = nova_assinatura
                log.info("--- Nova questão detectada (posição %d) ---", indice + 1)
                ia.enviar(*resultado)

            except StaleElementReferenceException:
                # Página re-renderizou entre o find_elements e a leitura; tenta de novo no próximo poll.
                continue
            except Exception as e:
                log.error("Erro ao processar questão: %s", e)
                continue

    return True


def monitorar(driver, ia, parar):
    """Loop de varredura. Para quando `parar` é sinalizado ou a conexão com o Chrome cai."""
    estado_perguntas = {}  # (janela, indice_na_pagina) -> assinatura da última questão tratada nela
    try:
        while not parar.is_set():
            if not varrer_abas(driver, ia, estado_perguntas):
                break
            parar.wait(POLL_SECONDS)
    finally:
        parar.set()  # fecha a janela também, se a conexão caiu


def criar_janela(parar):
    try:
        return JanelaResposta(parar)
    except Exception as e:  # sem tkinter instalado ou sem ambiente gráfico
        log.warning("Sem interface gráfica (%s); as respostas vão aparecer só no terminal.", e)
        return None


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    client = criar_cliente()
    if client is None:
        log.error("GROQ_API_KEY não definida. Crie um .env a partir de chave.env.example.")
        return 1

    if not garantir_chrome():
        return 1

    try:
        driver = conectar_chrome()
    except Exception as e:
        log.error("Erro ao conectar ao Chrome em %s: %s", DEBUGGER_ADDRESS, e)
        return 1
    log.info("Conectado ao Chrome com sucesso!")

    parar = threading.Event()
    janela = criar_janela(parar)
    ia = IAWorker(client, janela or SaidaTerminal())

    log.info("Monitorando... Abra a página das questões no Chrome que foi aberto.")
    log.info(
        "(poll a cada %ss; cobre todas as abas abertas e todas as questões de cada página)",
        POLL_SECONDS,
    )

    if janela is None:
        try:
            monitorar(driver, ia, parar)
        except KeyboardInterrupt:
            pass
    else:
        # O Tk fica na thread principal; a varredura vai para uma thread própria.
        # Ctrl+C no terminal e fechar a janela encerram do mesmo jeito.
        signal.signal(signal.SIGINT, lambda *_: parar.set())
        threading.Thread(
            target=monitorar, args=(driver, ia, parar), name="monitor", daemon=True
        ).start()
        janela.rodar()

    log.info("Encerrado. O Chrome continua aberto para a próxima execução.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
