# Fichier : config.py
import os

DOSSIER_ACTUEL = os.path.dirname(os.path.abspath(__file__))
CHEMIN_BASE = os.path.join(DOSSIER_ACTUEL, "banque.db")

PREFIXES = {
    "Seconde": "SEC",
    "Première spé": "PRE",
    "Terminale spé": "TER",
    "Maths expertes": "EXP",
    "Première ST2S": "ST2S",
    "Terminale ST2S": "TST2S",
    "BTS SIO 1": "BTS1",
    "BTS SIO 2": "BTS2"
}

def prochain_id(prefixe, cursor):
    cursor.execute("SELECT id FROM exercices WHERE id LIKE ?", (prefixe + "-%",))
    ids = cursor.fetchall()
    if not ids:
        return f"{prefixe}-001"
    numeros = [int(i[0].split("-")[1]) for i in ids]
    return f"{prefixe}-{max(numeros) + 1:03d}"