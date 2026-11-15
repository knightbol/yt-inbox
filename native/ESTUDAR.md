# Estudar o yt-inbox em C

O programa organiza uma caixa de entrada de vídeos. A sua responsabilidade é
guardar escolhas, apresentar listas e coordenar ferramentas externas. O yt-dlp
conhece o YouTube; o mpv conhece codecs e placas gráficas. Esta separação permite
estudar o núcleo sem implementar um leitor multimédia.

## Ordem de leitura

| Ficheiro | O que estudar |
|---|---|
| `inbox.h` | Estruturas, tipos, contratos das funções e propriedade da memória. |
| `main.c` | Entrada do programa, argumentos e encaminhamento dos comandos. |
| `schema.sql` | Tabelas, relações e estados permitidos. |
| `store.c` | Consultas, transações, alterações de estado e undo. |
| `tui.c` | Ciclo de eventos, teclas, páginas, desenho e suspensão do terminal. |
| `feeds.c` | URLs, parsing XML, validação, feeds, arquivo e Shorts. |
| `util.c` | Ficheiros, JSON, memória, processos, sinais e HTTPS. |
| `player.c` | Argumentos do mpv, descargas e validação de ficheiros locais. |
| `sync.c` | Relógios lógicos, conflitos e projeção de estado para channels.txt. |
| `tmux.c` | Entrega do terminal real ao leitor e regresso à sessão. |
| `install.c` | Instalação por utilizador e criação dos serviços. |
| `embed.c` | Pequena ferramenta de compilação que incorpora o SQL no binário. |

Começa por seguir um caminho completo: `watched VIDEO_ID` em `main.c`, depois
`set_status()` em `store.c`. A função abre uma transação, guarda a ação anterior,
altera o vídeo e regista uma revisão de sincronização. Só depois confirma a
transação. Se alguma operação falhar, faz rollback.

## Memória e valores de retorno

Uma função que devolve `char *` normalmente entrega memória ao chamador. Essa
memória precisa de `free()`. Uma função que devolve `Json *` normalmente entrega
uma referência que precisa de `json_object_put()`.

`jget()`, `jfirst()` e `jstr()` são exceções: devolvem referências ou apontadores
emprestados. Não os libertes, nem os uses depois de libertar o objeto de origem.
`json_object_object_add()` passa a ser dono do valor recebido. Para partilhar um
valor existente, incrementa primeiro a referência com `json_object_get()`.

O objeto `App` reúne os caminhos, a ligação SQLite, as definições e a última
mensagem de erro. As funções devolvem normalmente `0` em caso de sucesso e `-1`
em caso de erro; funções que devolvem objetos usam `NULL`. Existem exceções
documentadas, como `ingest()`, que devolve o número de vídeos acrescentados.

Os rótulos `done` e `rollback` concentram a limpeza dos recursos. Em C não há
recolha automática de lixo: cada saída da função precisa de respeitar a
propriedade dos recursos que já adquiriu.

## SQL e processos

`db_exec()` e `db_rows()` usam `sqlite3_vmprintf()`. Os formatos são constantes
internas; `%Q` coloca um valor textual entre aspas SQL e escapa as aspas internas.
Não substituas `%Q` por `%s` para inserir texto do utilizador numa consulta.
`%s` fica reservado a fragmentos SQL constantes controlados pelo programa.

Os programas externos recebem um vetor `argv`, terminado por `NULL`, através de
`execvp()`. Um título de vídeo nunca é código shell. O `--` separa opções do URL.
O caso especial de tmux precisa de uma cadeia shell para `detach-client -E`;
por isso usa uma função específica de quoting.

`process()` usa um pipe, `fork()`, `poll()` e `waitpid()`. Limita a saída e o tempo
de execução. Uma descarga cria um grupo de processos próprio para que o
cancelamento também alcance o processo de junção de áudio/vídeo.

## Estado e sincronização

`pending`, `watched`, `skipped` e `baseline` são estados guardados na base de
dados. `baseline` permite conhecer vídeos antigos sem encher automaticamente a
caixa de entrada. Abrir o mpv ou terminar uma descarga nunca marca um vídeo como
visto.

A sincronização trabalha com registos individuais. Cada alteração explícita
recebe `[contador, identificador_do_dispositivo]`. O contador evita depender da
hora dos dois computadores. A primeira junção de históricos antigos dá
prioridade a `watched`, depois `skipped`, depois `pending`.

Segue estas funções por ordem: `sync_snapshot()`, `sync_merge()`, `sync_apply()`
e `sync_now()`. A base de dados é confirmada antes de escrever channels.txt. Se
o programa parar entre as duas operações, a próxima execução volta a projetar
os registos guardados. Nenhuma transação SQLite fica aberta durante pedidos HTTP.

## Compilar, experimentar e depurar

Na raiz do repositório:

```sh
make -C native
./native/build/yt-inbox-c --help
YT_INBOX_HOME="$PWD/native-demo" ./native/build/yt-inbox-c tui
```

Esta variável cria um perfil de estudo separado do teu histórico habitual. Usa
esse perfil também quando executares comandos de teste ou depuração.

Para compilar sem otimizações, facilitando a leitura no depurador:

```sh
make -C native clean
make -C native CFLAGS='-O0 -g3 -std=c11 -Wall -Wextra -Wpedantic'
YT_INBOX_HOME="$PWD/native-demo" gdb --args ./native/build/yt-inbox-c list
```

No GDB: `break set_status`, `run`, `next`, `step`, `print` e `backtrace` permitem
seguir a execução. `make -C native sanitize` compila com AddressSanitizer e
UndefinedBehaviorSanitizer e executa os testes. O Python é usado nos testes para
comparar os resultados com a implementação anterior; não é chamado pelo núcleo C.

## Exercícios pequenos

1. Acrescentar um comando que conte vídeos em cada estado, sem escrever na base.
2. Mostrar a data de publicação na TUI; tratar separadamente os vídeos sem data.
3. Acrescentar uma tecla à ajuda e seguir o evento até uma ação existente.
4. Simular uma falha de yt-dlp e observar quais os estados que ficam guardados.
5. Criar dois perfis temporários nos testes, alterar estados e verificar a junção.

Mantém os testes de compatibilidade quando alterares a sincronização. Uma
reescrita que apresenta a mesma lista pode ainda discordar da outra versão na
resolução de um conflito ou depois de uma interrupção.
