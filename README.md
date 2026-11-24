# Resolução Automática de Questões com IA e Selenium

Script em Python que monitora uma página web (por exemplo, um questionário no Moodle) já aberta no Chrome, identifica automaticamente a questão de múltipla escolha exibida, envia o enunciado e as alternativas para um modelo de linguagem e apresenta a resposta sugerida numa pequena janela que fica sempre no topo.

## Como funciona

Ao iniciar, o script procura um Chrome já aberto na porta de depuração. Se não houver, ele mesmo abre o Chrome com a porta habilitada e um perfil próprio do projeto (`perfil-chrome/`), e se conecta a ele pelo Chrome DevTools Protocol. A cada poll (intervalo configurável), ele:

1. Percorre todas as abas abertas no navegador.
2. Localiza blocos de questão (elementos com a classe `qtext`, padrão do Moodle) em cada aba.
3. Extrai o enunciado e as alternativas associadas a cada questão.
4. Confirma a leitura com uma segunda verificação, evitando processar conteúdo capturado no meio de um re-render da página.
5. Calcula uma assinatura (hash) da questão para não reprocessar a mesma pergunta repetidamente.
6. Envia o enunciado e as alternativas para a API da Groq (compatível com a API da OpenAI) numa thread separada, para a varredura não travar esperando a resposta.
7. Mostra a resposta sugerida numa janela única feita com Tkinter, que é atualizada a cada questão em vez de abrir um popup novo.

As respostas ficam guardadas por assinatura durante a execução: se a mesma questão aparecer de novo (em outra aba, ou ao voltar para ela), a janela usa a resposta guardada sem chamar a API outra vez.

## Tecnologias utilizadas

- Python 3
- Selenium (conexão com o Chrome via CDP)
- Tkinter (janela de respostas)
- SDK da OpenAI, apontado para a API da Groq
- python-dotenv (carregamento de variáveis de ambiente)

## Pré-requisitos

- Python 3.10 ou superior.
- Google Chrome instalado.
- Uma chave de API válida da Groq (https://console.groq.com).

## Instalação

Clone o repositório e instale as dependências:

```bash
git clone <url-do-repositorio>
cd REPO
pip install -r requirements.txt
```

## Configuração

Crie um arquivo `.env` (ou `key.env`) na raiz do projeto com a sua chave da Groq, seguindo o modelo em `chave.env.example`:

```
GROQ_API_KEY=sua_chave_aqui
```

Esse arquivo é ignorado pelo Git (ver `.gitignore`) e não deve ser versionado.

Variáveis opcionais no `.env`:

| Variável | Padrão | Para que serve |
|---|---|---|
| `GROQ_MODEL` | `openai/gpt-oss-120b` | Modelo usado na Groq |
| `CHROME_PATH` | detectado automaticamente | Caminho do executável do Chrome, se não for encontrado sozinho |
| `CHROME_PORT` | `9222` | Porta de depuração remota |
| `CHROME_PROFILE_DIR` | `perfil-chrome` | Pasta do perfil usado pelo Chrome aberto pelo script |
| `URL_INICIAL` | nenhuma | Página aberta junto com o Chrome (ex.: o Moodle) |
| `POLL_SECONDS` | `2` | Intervalo entre as varreduras |
| `CHROME_START_TIMEOUT` | `20` | Segundos de espera pela porta depois de abrir o Chrome |

## Uso

```bash
python main.py
```

1. O script abre o Chrome sozinho (ou reaproveita um que ele já tenha aberto antes).
2. Na primeira vez, faça login no Moodle nessa janela. O perfil fica salvo em `perfil-chrome/`, então nas próximas execuções o login já estará lá.
3. Abra a página das questões. Ao detectar uma questão nova, o script consulta a IA e atualiza a janela de respostas. Quando a letra é reconhecida, ela mostra a alternativa completa (por exemplo, `B) Roma`); senão, mostra a resposta crua.

A janela de respostas:

- mostra o estado atual ("Aguardando questão...", "Consultando a IA...", "Respondida às 10:30:16" ou um erro em vermelho), a resposta em destaque e o início do enunciado;
- tem a opção **Sempre no topo** (ligada por padrão) e um botão **Copiar resposta**;
- volta a aparecer sozinha quando chega uma resposta nova, se estiver minimizada.

Para encerrar, feche a janela ou use `Ctrl+C` no terminal. O Chrome continua aberto e a próxima execução só se reconecta a ele. Sem ambiente gráfico (ou sem Tkinter), o script continua funcionando e as respostas aparecem só no terminal.

### Por que um perfil separado?

Desde o Chrome 136, a opção `--remote-debugging-port` é ignorada no perfil padrão do Chrome (é uma proteção contra roubo de cookies). Ela só funciona com `--user-data-dir` apontando para outra pasta, por isso o script usa `perfil-chrome/`. Esse perfil roda em paralelo ao seu Chrome normal, que pode continuar aberto.

Se preferir abrir o Chrome manualmente, use as duas opções juntas:

```bash
chrome --remote-debugging-port=9222 --user-data-dir="caminho/para/perfil-chrome"
```

## Estrutura do projeto

```
REPO/
├── main.py                 # Script principal
├── tests/test_main.py      # Testes unitários (pytest)
├── pytest.ini              # Configuração do pytest
├── requirements.txt        # Dependências do projeto
├── chave.env.example       # Modelo de arquivo de variáveis de ambiente
├── .github/workflows/      # Workflow de CI (GitHub Actions)
└── .gitignore
```

## Testes e integração contínua

```bash
pip install -r requirements.txt pytest
python -m pytest
```

O workflow `.github/workflows/ci.yml` roda esses testes a cada push na `main` e em pull requests. Eles cobrem a lógica pura (interpretação da resposta da IA, montagem do prompt, assinatura da questão), a localização e o comando de abertura do Chrome, o `IAWorker` (cache, deduplicação e erros), a varredura das abas com um driver falso e a janela de respostas. Os testes da janela são pulados automaticamente sem ambiente gráfico, como no CI; para rodá-los no Linux sem tela, use `xvfb-run python -m pytest`. O fluxo completo com o Chrome não roda em CI, pois depende de uma instância local do navegador.

O Tkinter acompanha o Python no Windows e no macOS; no Linux pode ser preciso instalar o pacote do sistema (por exemplo, `sudo apt install python3-tk`).

## Limitações conhecidas

- A extração de alternativas foi ajustada para a estrutura HTML do Moodle e pode não funcionar em outras plataformas sem adaptação.
- Quando há múltiplas questões sem um contêiner identificável (`div.que`) na mesma página, o fallback de busca global de alternativas só é aplicado se houver exatamente uma questão na página, para evitar misturar alternativas de questões diferentes.
- O Chrome aberto pelo script usa um perfil próprio: extensões, favoritos e logins do seu perfil normal não estão nele.
- O cache de respostas vive só na memória; ao reiniciar o script, ele começa vazio.
- Questões de múltipla resposta (checkbox), associação e enunciados com imagem não são tratados.

## Aviso

Este projeto tem finalidade educacional, para estudo de automação de navegador e integração com APIs de IA. O uso para obter respostas em avaliações reais pode violar as regras acadêmicas ou institucionais aplicáveis. O uso é de responsabilidade exclusiva de quem executa o script.
