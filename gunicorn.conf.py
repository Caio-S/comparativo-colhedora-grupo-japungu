# A busca de dados roda numa thread de fundo dentro do mesmo processo do worker e
# disputa CPU com a thread principal (que avisa o Gunicorn que esta viva). O timeout
# padrao de 30s e curto demais para isso em instancias com CPU limitada -- o Gunicorn
# matava e reiniciava o worker no meio da primeira busca, resetando o cache sempre que
# estava quase pronto.
timeout = 600
