# pm-recon -- разведка перед личным аналитическим слоем над Polymarket

Один пользователь, один VPS, только чтение. Этот архив НЕ торгует и НЕ
содержит логики исполнения. Его единственная задача -- закрыть четыре
инфраструктурные неизвестные до того, как будет потрачено время на Цель B.

## Порядок чтения

1. `PREREGISTRATION.md` -- критерий GO, три исхода, два гейта, правило чтения.
2. `ASSUMPTIONS.md` -- где ваши вводные данные могут быть неверны.
3. `probe.py` -- что именно измеряется.

## Структура

```
pm/
  config.py      константы инфраструктуры + Settings (PM_*)
  fees.py        ЕДИНСТВЕННЫЙ источник чисел комиссии
  httpc.py       read-only HTTP с retry, throttle, сырым журналом JSONL
  markets.py     парсинг рынков Gamma
  stats.py       кластерные SE, шринкаж (EB), BH-FDR, decide()
  store.py       JSON/Parquet/DuckDB артефакты
  experiments/
    e1_prices_history.py   семантика /prices-history
    e2_fee_basis.py        базис C в формуле комиссии
    e3_gtd_cancel.py       GTD/TTL, p99 DELETE, риск failed settlement
    e4_tennis.py           объём/разрешения/споры UMA + гейт мощности
probe.py         CLI
tests/           юнит-тесты fees и stats (stdlib unittest)
```

Файла `pm/broker.py` в архиве нет намеренно -- см. `ASSUMPTIONS.md`, раздел A12.

## Запуск

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # заполнить PM_SELF_ADDRESSES обязательно

python probe.py --all                 # Э1 + Э2 + Э4 + Э3(readonly)
python probe.py --e1 --hours 24 --markets 16
python probe.py --e2 --tx 0x...       # ончейн-путь Э2
python -m unittest discover -s tests -v
```

Живой тест Э3 требует `--run-e3`, `--token-id`, реализованного `pm/broker.py`
и ввода фразы `I ACCEPT ORDER PLACEMENT RISK` с клавиатуры.

## Три правила, нарушение которых обесценивает результат

1. Числа комиссий только из `pm/fees.py`. Нигде больше.
2. Артефакты в `data/` не перезаписываются: каждый прогон -- новый файл с UTC-меткой.
3. `PREREGISTRATION.md` после заморозки меняется только разделом ПОПРАВКИ.

## Соглашение о дальнейшей работе

Дальше присылаются только ИЗМЕНЁННЫЕ файлы с указанием пути, а не весь проект.
