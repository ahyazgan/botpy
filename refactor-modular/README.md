# refactor-modular

`claude/start-from-scratch-DMbV8` dalındaki **modüler mimari denemesi**. Eski
monolitik `bot.py`/`arb_bot.py`/`api.py` yapısını `config / models / fetcher /
scanner / screener / trader / app / main` modüllerine bölen sıfırdan iskelet.

Birleşik ağaca aynen alındığında çalışan dosyaları sileceği için buraya izole
edildi; değerlendirilip benimsenirse kök dizine taşınabilir. Tam geçmiş:
`git log claude/start-from-scratch-DMbV8`.
