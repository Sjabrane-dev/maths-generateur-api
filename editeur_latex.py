# Fichier : API_RENDER/editeur_latex.py
# Éditeur LaTeX du site : compile un document envoyé par le navigateur.
#
# SÉCURITÉ — à lire :
#  * Mot de passe FACULTATIF : sans variable EDITEUR_EMPREINTE sur Render,
#    l'éditeur est ouvert à tous. Pour le protéger plus tard, créer sur Render
#    la variable EDITEUR_EMPREINTE contenant l'empreinte SHA-256 du mot de
#    passe (commande : printf '%s' 'MonMotDePasse' | sha256sum).
#  * pdflatex tourne sans shell-escape, en mode « paranoïaque » pour les
#    fichiers (ni chemins absolus, ni « .. », ni fichiers cachés), avec une
#    limite de temps, une taille maximale et une seule compilation à la fois.
import hashlib
import hmac
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

TAILLE_MAX = 60_000          # caractères maximum dans un document
DELAI_MAX = 25               # secondes maximum par passe de pdflatex
PASSES_MAX = 2
LIMITE_PAR_MINUTE = 12       # compilations par adresse IP et par minute

# Commandes refusées d'emblée (en plus des protections de TeX Live)
INTERDITS = [
    r"\\write18", r"\\immediate\s*\\write", r"\\openout", r"\\openin", r"\\read\b",
    r"\\directlua", r"\\luaexec", r"\\ShellEscape", r"\\usepackage\s*(\[[^\]]*\])?\s*\{\s*(shellesc|pythontex|minted|bashful)\s*\}",
    r"\\input\s*\{?\s*[/~]", r"\\include\s*\{?\s*[/~]", r"\\(input|include|includegraphics)[^\n]*\.\.",
]

_verrou = threading.Semaphore(1)
_historique: Dict[str, List[float]] = {}

router = APIRouter()


class DemandeCompilation(BaseModel):
    code: str = Field(min_length=1, max_length=TAILLE_MAX)
    passes: int = Field(default=1, ge=1, le=PASSES_MAX)


def mot_de_passe_valide(mot_de_passe: Optional[str]) -> bool:
    attendu = os.environ.get("EDITEUR_EMPREINTE", "").strip().lower()
    if not attendu:
        return True          # pas de variable sur Render : éditeur ouvert à tous
    if not mot_de_passe:
        return False
    empreinte = hashlib.sha256(mot_de_passe.encode("utf-8")).hexdigest()
    return hmac.compare_digest(empreinte, attendu)


def commande_interdite(code: str) -> Optional[str]:
    for motif in INTERDITS:
        m = re.search(motif, code)
        if m:
            return m.group(0)
    return None


def extraire_erreurs(journal: str) -> List[dict]:
    """Repère les erreurs « ! … » du journal pdflatex et leur numéro de ligne."""
    erreurs = []
    lignes = journal.splitlines()
    for i, ligne in enumerate(lignes):
        if ligne.startswith("!") and "==> Fatal error" not in ligne:
            numero = None
            extrait = ""
            for suivante in lignes[i + 1:i + 12]:
                m = re.match(r"l\.(\d+)\s?(.*)", suivante)
                if m:
                    numero = int(m.group(1))
                    extrait = m.group(2).strip()
                    break
            erreurs.append({"message": ligne[1:].strip(), "ligne": numero, "extrait": extrait})
            if len(erreurs) >= 10:
                break
    return erreurs


def compiler_document(code: str, passes: int = 1) -> Tuple[Optional[bytes], List[dict], str]:
    """Compile le code ; renvoie (pdf ou None, erreurs, fin du journal)."""
    dossier = tempfile.mkdtemp(prefix="editeur_")
    env = dict(os.environ, openin_any="p", openout_any="p", shell_escape="f")
    try:
        chemin_tex = os.path.join(dossier, "document.tex")
        with open(chemin_tex, "w", encoding="utf-8") as f:
            f.write(code)
        res = None
        for _ in range(passes):
            try:
                res = subprocess.run(
                    ["pdflatex", "-interaction=nonstopmode", "-halt-on-error",
                     "-no-shell-escape", "document.tex"],
                    capture_output=True, cwd=dossier, timeout=DELAI_MAX, env=env,
                )
            except subprocess.TimeoutExpired:
                return None, [{"message": "La compilation a pris trop de temps (boucle infinie ?).",
                               "ligne": None, "extrait": ""}], ""
            if res.returncode != 0:
                break
        journal = ""
        chemin_log = os.path.join(dossier, "document.log")
        if os.path.exists(chemin_log):
            with open(chemin_log, encoding="utf-8", errors="replace") as f:
                journal = f.read()
        chemin_pdf = os.path.join(dossier, "document.pdf")
        if res is not None and res.returncode == 0 and os.path.exists(chemin_pdf):
            with open(chemin_pdf, "rb") as f:
                return f.read(), [], journal[-2000:]
        erreurs = extraire_erreurs(journal) or [{"message": "La compilation a échoué.", "ligne": None, "extrait": ""}]
        return None, erreurs, journal[-3000:]
    finally:
        shutil.rmtree(dossier, ignore_errors=True)


def _trop_de_demandes(ip: str) -> bool:
    maintenant = time.time()
    liste = [t for t in _historique.get(ip, []) if maintenant - t < 60]
    if len(liste) >= LIMITE_PAR_MINUTE:
        _historique[ip] = liste
        return True
    liste.append(maintenant)
    _historique[ip] = liste
    return False


@router.post("/compiler-latex")
def api_compiler_latex(demande: DemandeCompilation,
                       x_mot_de_passe: Optional[str] = Header(default=None),
                       x_forwarded_for: Optional[str] = Header(default=None)):
    if not mot_de_passe_valide(x_mot_de_passe):
        time.sleep(1)   # ralentit les essais au hasard
        raise HTTPException(status_code=401, detail="Mot de passe incorrect.")

    ip = (x_forwarded_for or "inconnue").split(",")[0].strip()
    if _trop_de_demandes(ip):
        raise HTTPException(status_code=429, detail="Trop de compilations : patientez une minute.")

    interdit = commande_interdite(demande.code)
    if interdit:
        raise HTTPException(status_code=400, detail=f"Commande non autorisée dans l'éditeur : {interdit}")

    if not _verrou.acquire(timeout=30):
        raise HTTPException(status_code=429, detail="Le serveur compile déjà un document : réessayez dans un instant.")
    try:
        pdf, erreurs, journal = compiler_document(demande.code, demande.passes)
    finally:
        _verrou.release()

    if pdf is None:
        raise HTTPException(status_code=422, detail={"erreurs": erreurs, "journal": journal})
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": 'inline; filename="document.pdf"'})


@router.post("/verifier-mot-de-passe")
def api_verifier(x_mot_de_passe: Optional[str] = Header(default=None)):
    if not mot_de_passe_valide(x_mot_de_passe):
        time.sleep(1)
        raise HTTPException(status_code=401, detail="Mot de passe incorrect.")
    return {"ok": True}
