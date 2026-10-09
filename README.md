# F1 Live

App personale sulla Formula 1: calendario, risultati, classifiche, statistiche di piloti e squadre, pronostici di
qualifica, sprint e gara, probabilità di titolo e storico dei pronostici.

Il sito si aggiorna da solo con GitHub Actions (`.github/workflows/aggiorna.yml`): ogni 30 minuti, ogni 15 nei
weekend di gara. Dati: Jolpica, OpenF1, Open-Meteo, MultiViewer.

Rami: `main` codice, `dati` dati da cui riparte ogni aggiornamento, `sito` il sito pubblicato, `storico` una copia
al giorno dello storico dei pronostici.
