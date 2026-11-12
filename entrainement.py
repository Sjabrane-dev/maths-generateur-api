# Fichier : API_RENDER/entrainement.py
# Feuilles d'entraînement pour les élèves : exercices tirés au hasard dans
# banque.db pour un chapitre, avec le corrigé sur des pages séparées
# ou dans un PDF à part (partie = "enonces" puis "corrige" avec la même graine).
#
# Intégration dans main.py (2 lignes) :
#     from entrainement import router as routeur_entrainement
#     app.include_router(routeur_entrainement)
#
# Protections : 6 exercices maximum, 6 feuilles par minute et par adresse IP,
# une seule compilation à la fois pour ce service.
import difflib
import random
import re
import shutil
import sqlite3
import threading
import time
import unicodedata
from contextlib import closing
from typing import Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

# Si le générateur change un jour de forme, ce service se désactive (503)
# au lieu d'empêcher toute l'API de démarrer.
try:
    from config import CHEMIN_BASE
    from generateur import (generer_preambule, FOOTER, titre_vers_latex,
                            echapper_latex, annee_scolaire, _compiler)
    DISPONIBLE = True
except Exception as erreur_import:          # pragma: no cover
    print("⚠️ Feuilles d'entraînement désactivées :", erreur_import)
    DISPONIBLE = False

NIVEAUX = {"Seconde", "Première ST2S", "Première spé", "Terminale spé",
           "Maths expertes", "Terminale ST2S", "BTS SIO 1", "BTS SIO 2"}
NOMBRE_MAX = 6
LIMITE_PAR_MINUTE = 6

_verrou = threading.Semaphore(1)
_historique: Dict[str, List[float]] = {}

router = APIRouter()


class DemandeEntrainement(BaseModel):
    niveau: str = Field(max_length=40)
    numero: Optional[str] = Field(default="", max_length=10)        # ex. "7" ou "7bis"
    titre_chapitre: Optional[str] = Field(default="", max_length=150)
    nombre: int = Field(default=4, ge=1, le=NOMBRE_MAX)
    difficulte: int = Field(default=0, ge=0, le=3)                   # 0 = mélange
    # "tout" : énoncés + corrigé à la fin ; "enonces" ou "corrige" : une seule partie.
    # Avec la même graine, « enonces » et « corrige » portent sur les mêmes exercices.
    partie: Optional[str] = Field(default="tout", max_length=8)
    graine: Optional[int] = Field(default=None, ge=0, le=2_000_000_000)


def _normaliser(texte: str) -> str:
    t = unicodedata.normalize("NFD", (texte or "").lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def _trouver_chapitre(conn, niveau: str, numero: str, titre: str) -> Optional[str]:
    """Titre du chapitre de banque.db : par titre exact, puis numéro, puis titre approché."""
    rangs = conn.execute("SELECT numero, titre FROM chapitres WHERE classe = ? ORDER BY numero",
                         (niveau,)).fetchall()
    if not rangs:
        return None
    voulu = _normaliser(titre)
    for _, t in rangs:
        if voulu and _normaliser(t) == voulu:
            return t
    if numero and re.fullmatch(r"\d+", numero.strip()):
        for n, t in rangs:
            if str(n).strip() == numero.strip():
                # on vérifie que le titre ne contredit pas le numéro (cas des « bis »)
                if not voulu or difflib.SequenceMatcher(None, voulu, _normaliser(t)).ratio() > 0.35:
                    return t
    if voulu:
        meilleur = max(rangs, key=lambda r: difflib.SequenceMatcher(None, voulu, _normaliser(r[1])).ratio())
        if difflib.SequenceMatcher(None, voulu, _normaliser(meilleur[1])).ratio() > 0.5:
            return meilleur[1]
    return None


def _trop_de_demandes(ip: str) -> bool:
    maintenant = time.time()
    liste = [t for t in _historique.get(ip, []) if maintenant - t < 60]
    trop = len(liste) >= LIMITE_PAR_MINUTE
    if not trop:
        liste.append(maintenant)
    _historique[ip] = liste
    return trop


def construire_latex(niveau: str, chapitre: str, exercices, partie: str = "tout") -> str:
    titre = ("Corrigé — " if partie == "corrige" else "Entraînement — ") + chapitre
    code = generer_preambule(titre if len(titre) <= 80 else "Feuille d'entraînement", "Les Jardins des Délices mathématiques")
    code += r"""
\thispagestyle{premiere}
\noindent
\begin{minipage}[t]{0.6\textwidth}
    \textbf{Les Jardins des Délices mathématiques} \\
    Année scolaire """ + annee_scolaire() + r"""
\end{minipage}
\begin{minipage}[t]{0.4\textwidth}
    \raggedleft
    \textbf{""" + echapper_latex(niveau) + r"""} \\
    Feuille tirée au hasard
\end{minipage}

\vspace{0.3cm}
\noindent{\color{col1}\rule{\textwidth}{0.8pt}}
\begin{center}
    {\LARGE \bfseries \color{col1} """ + ("Corrigé de la feuille d'entraînement" if partie == "corrige" else "Feuille d'entraînement") + r"""}\\[2mm]
    {\large """ + echapper_latex(chapitre) + r"""}\\[1mm]
    {\small\itshape """ + {"tout": "Cherche d'abord seul(e) : le corrigé est à la fin.",
                             "enonces": "Cherche d'abord seul(e) : le corrigé est dans un fichier à part.",
                             "corrige": "À lire après avoir cherché les exercices !"}.get(partie, "") + r"""}
\end{center}
\noindent{\color{col1}\rule{\textwidth}{1.5pt}}
"""
    if partie != "corrige":
        for _id, titre_ex, _diff, enonce, _corr in exercices:
            code += "\\begin{exercice}{" + titre_vers_latex(titre_ex) + "}\n" + enonce + "\n\\end{exercice}\n"
    if partie == "enonces":
        return code + FOOTER

    if partie != "corrige":
        code += r"""
\newpage
\begin{center}{\LARGE\bfseries\color{col1} Corrigé}\end{center}
\noindent{\color{col1}\rule{\textwidth}{1.5pt}}
\setcounter{exocount}{0}
"""
    for _id, titre_ex, _diff, _enonce, corr in exercices:
        texte = corr if (corr or "").strip() else r"\textit{Corrigé non disponible pour cet exercice.}"
        code += "\\begin{exercice}{" + titre_vers_latex(titre_ex) + "}\n" + texte + "\n\\end{exercice}\n"
    return code + FOOTER


@router.post("/feuille-entrainement")
def api_feuille_entrainement(demande: DemandeEntrainement,
                             x_forwarded_for: Optional[str] = Header(default=None)):
    if not DISPONIBLE:
        raise HTTPException(status_code=503, detail="Service indisponible.")
    if demande.niveau not in NIVEAUX:
        raise HTTPException(status_code=400, detail="Classe inconnue.")
    ip = (x_forwarded_for or "inconnue").split(",")[0].strip()
    if _trop_de_demandes(ip):
        raise HTTPException(status_code=429, detail="Trop de feuilles demandées : patiente une minute.")

    with closing(sqlite3.connect(CHEMIN_BASE)) as conn:
        chapitre = _trouver_chapitre(conn, demande.niveau, demande.numero or "", demande.titre_chapitre or "")
        if not chapitre:
            raise HTTPException(status_code=404, detail="Ce chapitre n'a pas encore d'exercices dans la banque.")
        lignes = conn.execute(
            "SELECT id, titre, difficulte, enonce, correction FROM exercices WHERE niveau = ? AND chapitre = ? ORDER BY id",
            (demande.niveau, chapitre)).fetchall()

    if demande.difficulte:
        choisis = [l for l in lignes if l[2] == demande.difficulte]
        if len(choisis) < demande.nombre:      # pas assez à ce niveau : on complète avec les autres
            choisis += [l for l in lignes if l not in choisis]
    else:
        choisis = list(lignes)
    if not choisis:
        raise HTTPException(status_code=404, detail="Ce chapitre n'a pas encore d'exercices dans la banque.")
    # priorité aux exercices qui ont un corrigé
    avec = [l for l in choisis if (l[4] or "").strip()]
    sans = [l for l in choisis if not (l[4] or "").strip()]
    partie = demande.partie if demande.partie in ("tout", "enonces", "corrige") else "tout"
    hasard = random.Random(demande.graine) if demande.graine is not None else random.Random()
    hasard.shuffle(avec); hasard.shuffle(sans)
    tirage = (avec + sans)[:demande.nombre]
    tirage.sort(key=lambda l: (l[2] or 0))      # du plus facile au plus difficile

    if not _verrou.acquire(timeout=40):
        raise HTTPException(status_code=429, detail="Le serveur est occupé : réessaie dans un instant.")
    try:
        chemin_pdf, dossier = _compiler(construire_latex(demande.niveau, chapitre, tirage, partie))
    except Exception:
        raise HTTPException(status_code=500, detail="La feuille n'a pas pu être fabriquée : réessaie (autre tirage).")
    finally:
        _verrou.release()

    return FileResponse(chemin_pdf, media_type="application/pdf",
                        filename="entrainement_corrige.pdf" if partie == "corrige" else "entrainement.pdf",
                        background=BackgroundTask(shutil.rmtree, dossier, ignore_errors=True))
