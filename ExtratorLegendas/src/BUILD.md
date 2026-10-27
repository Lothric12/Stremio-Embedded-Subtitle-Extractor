# Executar e compilar

Requer Python 3.12+ no Windows. O executável distribuído já inclui o runtime.

Na pasta `src`:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe app.py
.venv\Scripts\python.exe -m unittest -v test_extractor
```

Para compilar o executável:

```powershell
.venv\Scripts\python.exe -m pip install pyinstaller==6.22.3
.venv\Scripts\python.exe -m PyInstaller --noconfirm --onefile --windowed --name ExtratorLegendas app.py
```

O teste com vídeo FFmpeg é opcional e aparece como ignorado quando a fixture
`work/test-media.mkv` não está no workspace. Os outros testes geram estruturas
MKV determinísticas em memória e usam um servidor HTTP local com suporte a Range.

Não há telemetria, servidor remoto próprio ou armazenamento persistente de URLs.
O cliente faz requisições ao endereço colado e segue seus redirecionamentos.
TLS é validado pelo Requests; o programa não desativa a validação de certificados.
