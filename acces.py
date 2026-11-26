# Fichier : API_RENDER/acces.py
"""Mot de passe des générateurs (générateur PDF du lycée et générateur universitaire), vérifié PAR LE SERVEUR.

Le site envoie le mot de passe dans l'en-tête « X-Mot-de-passe » ; le serveur compare son empreinte SHA-256
à la variable d'environnement GENERATEUR_EMPREINTE définie sur Render (jamais écrite dans le site ni sur GitHub).

    Calcul de l'empreinte, dans un terminal :  printf '%s' 'MonMotDePasse' | sha256sum
    Sur Render : Environment > Add Environment Variable > GENERATEUR_EMPREINTE = les 64 caractères.

Sans cette variable, les générateurs restent ouverts à tous (comme avant).
Anti-devinette : après 10 essais faux en 15 minutes, l'adresse IP est bloquée 15 minutes.
"""
import hashlib
import hmac
import os
import time
from typing import Dict, List, Optional

from fastapi import HTTPException

ESSAIS_MAX = 10
FENETRE = 15 * 60          # secondes
_echecs: Dict[str, List[float]] = {}


def _attendue() -> str:
    return os.environ.get("GENERATEUR_EMPREINTE", "").strip().lower()


def acces_ok(mot_de_passe: Optional[str]) -> bool:
    attendu = _attendue()
    if not attendu:
        return True                     # pas de variable sur Render : générateurs ouverts
    if not mot_de_passe:
        return False
    empreinte = hashlib.sha256(mot_de_passe.encode("utf-8")).hexdigest()
    return hmac.compare_digest(empreinte, attendu)


def adresse(x_forwarded_for: Optional[str]) -> str:
    return (x_forwarded_for or "inconnue").split(",")[0].strip()


def _recents(ip: str) -> List[float]:
    maintenant = time.time()
    liste = [t for t in _echecs.get(ip, []) if maintenant - t < FENETRE]
    if liste:
        _echecs[ip] = liste
    else:
        _echecs.pop(ip, None)
    return liste


def exiger_acces(mot_de_passe: Optional[str], x_forwarded_for: Optional[str]) -> None:
    """Lève 401 (mot de passe absent ou faux) ou 429 (trop d'essais) ; ne fait rien si l'accès est bon."""
    if not _attendue():
        return
    ip = adresse(x_forwarded_for)
    if len(_recents(ip)) >= ESSAIS_MAX:
        raise HTTPException(status_code=429, detail="Trop d'essais : réessayez dans un quart d'heure.")
    if acces_ok(mot_de_passe):
        return
    if mot_de_passe:                    # un essai réel (pas un simple oubli d'en-tête) compte comme échec
        _echecs.setdefault(ip, []).append(time.time())
        time.sleep(1)
    if len(_echecs) > 5000:             # ménage
        for cle in list(_echecs):
            _recents(cle)
    raise HTTPException(status_code=401, detail="Mot de passe requis ou incorrect.")
