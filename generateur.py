# Fichier : API_RENDER/generateur.py
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from datetime import datetime

from config import CHEMIN_BASE

MAX_EXERCICES = 40        # limite par feuille (protège le serveur)
DELAI_COMPILATION = 60    # secondes maximum pour pdflatex


class ErreurGeneration(Exception):
    """La compilation LaTeX a échoué (erreur côté serveur)."""


PREAMBULE_BASE = r"""\documentclass[11pt,a4paper]{article}
\usepackage[utf8]{inputenc}
\usepackage[T1]{fontenc}
\usepackage{lmodern}
\usepackage[french]{babel}
\usepackage{amsmath,amsfonts,amssymb,geometry}
\geometry{hmargin=2cm,vmargin=2cm}

\newcommand{\N}{\mathbb{N}}
\newcommand{\Z}{\mathbb{Z}}
\newcommand{\Q}{\mathbb{Q}}
\newcommand{\R}{\mathbb{R}}
\newcommand{\D}{\mathbb{D}}
\newcommand{\C}{\mathbb{C}}

\newcounter{exocount}
\newenvironment{exercice}[1]{
    \stepcounter{exocount}
    \vspace{0.5cm}
    \noindent\textbf{Exercice \arabic{exocount} -- #1}
    \par\medskip
}{
    \par
}

\begin{document}
"""

FOOTER = r"""
\end{document}
"""

# ---------------------------------------------------------------------------
# Protection du LaTeX : tout texte venant de l'extérieur (titre, classe,
# établissement) est « échappé » pour ne pas casser la compilation
# (ni permettre d'injecter des commandes LaTeX).
# ---------------------------------------------------------------------------
_SPECIAUX = {
    "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$",
    "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
    "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
}

# repère les petites expressions comme  u_5  u_{n+1}  a^2  5^{n+1}
_MOT_MATHS = re.compile(r"[A-Za-z0-9]+(?:[_^](?:\{[^{}]*\}|[A-Za-z0-9]+))+")


def echapper_latex(texte):
    return "".join(_SPECIAUX.get(c, c) for c in (texte or ""))


def titre_vers_latex(titre):
    """Échappe un titre d'exercice et met en mode math les u_5, a^2, etc."""
    titre = titre or ""
    morceaux, pos = [], 0
    for m in _MOT_MATHS.finditer(titre):
        morceaux.append(echapper_latex(titre[pos:m.start()]))
        morceaux.append("$" + m.group(0) + "$")
        pos = m.end()
    morceaux.append(echapper_latex(titre[pos:]))
    return "".join(morceaux)


def annee_scolaire():
    now = datetime.now()
    debut = now.year if now.month >= 8 else now.year - 1
    return f"{debut}-{debut + 1}"


def recuperer_exercices(ids):
    """Renvoie {id: (titre, niveau, enonce)} pour les id trouvés en base."""
    if not ids:
        return {}
    marques = ",".join("?" * len(ids))
    with closing(sqlite3.connect(CHEMIN_BASE)) as conn:
        lignes = conn.execute(
            f"SELECT id, titre, niveau, enonce FROM exercices WHERE id IN ({marques})",
            ids,
        ).fetchall()
    return {l[0]: (l[1], l[2], l[3]) for l in lignes}


def assembler_et_compiler(liste_choix, titre_fiche="Feuille d'exercices",
                          classe_fiche="", etablissement_fiche=""):
    """Compile la feuille. Renvoie (chemin_pdf, dossier_temporaire).

    L'appelant doit supprimer le dossier temporaire une fois le PDF envoyé.
    Lève ValueError si la demande est invalide, ErreurGeneration si LaTeX échoue.
    """
    ids = list(dict.fromkeys(liste_choix))  # supprime les doublons, garde l'ordre
    if not ids:
        raise ValueError("Aucun exercice sélectionné.")
    if len(ids) > MAX_EXERCICES:
        raise ValueError(f"Maximum {MAX_EXERCICES} exercices par feuille.")

    trouves = recuperer_exercices(ids)
    introuvables = [i for i in ids if i not in trouves]
    if introuvables:
        print(f"⚠️ Exercices absents de la base : {introuvables}")
    if not trouves:
        raise ValueError("Aucun des exercices demandés n'existe dans la base.")

    lycee = (etablissement_fiche or "").strip().upper() or "LYCÉE POLYVALENT JACQUES BREL"
    if classe_fiche and classe_fiche.strip():
        niveau_affiche = classe_fiche.strip()
    else:  # déduit des exercices choisis
        niveaux = list(dict.fromkeys(trouves[i][1] for i in ids if i in trouves))
        niveau_affiche = " / ".join(niveaux)
    titre_fiche = (titre_fiche or "").strip() or "Feuille d'exercices"
    date_du_jour = datetime.now().strftime("%d/%m/%Y")

    code = PREAMBULE_BASE
    code += r"""
\noindent
\begin{minipage}[t]{0.5\textwidth}
    \textbf{""" + echapper_latex(lycee) + r"""} \\
    Année scolaire """ + annee_scolaire() + r"""
\end{minipage}
\begin{minipage}[t]{0.5\textwidth}
    \raggedleft
    \textbf{""" + echapper_latex(niveau_affiche) + r"""} \\
    \textbf{Date : } """ + date_du_jour + r"""
\end{minipage}

\vspace{0.2cm}
\noindent\rule{\textwidth}{0.5pt}
\vspace{0.3cm}

\begin{center}
    {\LARGE \bfseries """ + echapper_latex(titre_fiche) + r"""}
\end{center}

\vspace{0.1cm}
\noindent\rule{\textwidth}{1.5pt}
\vspace{0.4cm}

\noindent
\textbf{Nom : } \dots\dots\dots\dots\dots\dots\dots\dots\dots\dots\dots\dots\dots\dots\dots\quad
\textbf{Prénom : } \dots\dots\dots\dots\dots\dots\dots\dots\dots\dots\dots\dots\dots\dots\dots

\vspace{0.4cm}
"""
    for identifiant in ids:
        if identifiant not in trouves:
            continue
        titre, niveau, enonce = trouves[identifiant]
        code += "\\begin{exercice}{" + titre_vers_latex(titre) + " (" + echapper_latex(niveau) + ")}\n"
        code += enonce + "\n"
        code += "\\end{exercice}\n"
    code += FOOTER

    return _compiler(code)


def _compiler(code_tex):
    # chaque demande a son propre dossier : plus de collision entre deux élèves
    dossier = tempfile.mkdtemp(prefix="fiche_")
    try:
        chemin_tex = os.path.join(dossier, "feuille_exercices.tex")
        chemin_pdf = os.path.join(dossier, "feuille_exercices.pdf")
        with open(chemin_tex, "w", encoding="utf-8") as f:
            f.write(code_tex)

        print("⏳ Compilation du PDF via pdflatex...")
        try:
            res = subprocess.run(
                ["pdflatex", "-interaction=nonstopmode", "-halt-on-error",
                 "-no-shell-escape", "-output-directory", dossier, chemin_tex],
                capture_output=True, cwd=dossier, timeout=DELAI_COMPILATION,
            )
        except subprocess.TimeoutExpired:
            raise ErreurGeneration("La compilation LaTeX a pris trop de temps.")

        if res.returncode != 0 or not os.path.exists(chemin_pdf):
            sortie = res.stdout.decode("utf-8", errors="replace")
            print("❌ ERREUR LATEX DÉTECTÉE :\n" + sortie[-3000:])
            raise ErreurGeneration("La compilation LaTeX a échoué.")

        print("🎉 Succès ! Le fichier PDF est prêt.")
        return chemin_pdf, dossier
    except Exception:
        shutil.rmtree(dossier, ignore_errors=True)
        raise
