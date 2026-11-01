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
\usepackage{tikz}
% ===== Ajouts pour la banque « Maths expertes » (à coller avant \begin{document}) =====
\usepackage{tikz,multicol,array,eurosym,xcolor,listings}
\usetikzlibrary{calc,angles,quotes,positioning,shapes.geometric,arrows.meta}
\definecolor{col1}{RGB}{12, 102, 98}
\newcommand{\U}{\mathbb{U}}
\newcommand{\K}{\mathbb{K}}
\renewcommand{\Re}{\operatorname{Re}}
\renewcommand{\Im}{\operatorname{Im}}
\tikzset{gr/.style={circle,fill=col1,text=white,minimum size=6.5mm,inner sep=0pt,font=\footnotesize},
  lien/.style={line width=0.6pt},
  fl/.style={->,>=stealth,line width=0.6pt}}
\newcommand{\MarkovDeuxN}[6]{%
  \begin{tikzpicture}[baseline=-1mm]
    \node[gr] (a) at (0,0) {#1};
    \node[gr] (b) at (3,0) {#2};
    \draw[fl] (a) to[bend left=30] node[above,font=\small]{#4} (b);
    \draw[fl] (b) to[bend left=30] node[below,font=\small]{#5} (a);
    \draw[fl] (a) to[out=150,in=210,looseness=8] node[left,font=\small]{#3} (a);
    \draw[fl] (b) to[out=30,in=330,looseness=8] node[right,font=\small]{#6} (b);
  \end{tikzpicture}}
\newcommand{\MarkovDeux}[4]{\MarkovDeuxN{$A_1$}{$A_2$}{#1}{#2}{#3}{#4}}
\lstset{language=Python,basicstyle=\ttfamily\small,keepspaces=true,showstringspaces=false,columns=fullflexible,
  literate={é}{{\'e}}1 {è}{{\`e}}1 {à}{{\`a}}1 {ê}{{\^e}}1 {ç}{{\c c}}1}
\geometry{hmargin=2cm,vmargin=2cm}

\newcommand{\N}{\mathbb{N}}
\newcommand{\Z}{\mathbb{Z}}
\newcommand{\Q}{\mathbb{Q}}
\newcommand{\R}{\mathbb{R}}
\newcommand{\D}{\mathbb{D}}
\newcommand{\C}{\mathbb{C}}

\newcounter{exocount}
\newenvironment{exercice}[2][]{
    \stepcounter{exocount}
    \vspace{0.5cm}
    \noindent\textbf{Exercice \arabic{exocount}#1 -- #2}
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


def formater_points(x):
    """4.0 -> « 4 points », 2.5 -> « 2,5 points », 1 -> « 1 point »."""
    nombre = f"{x:g}".replace(".", ",")
    return f"{nombre}~point" + ("s" if x > 1 else "")


def nettoyer_points(points, trouves):
    """Garde uniquement les points valides (0 < x <= 100) des exercices trouvés."""
    propres = {}
    for cle, valeur in (points or {}).items():
        try:
            x = float(valeur)
        except (TypeError, ValueError):
            continue
        if cle in trouves and 0 < x <= 100:
            propres[cle] = x
    return propres


def assembler_et_compiler(liste_choix, titre_fiche="Feuille d'exercices",
                          classe_fiche="", etablissement_fiche="", duree_fiche="",
                          points=None):
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
    duree = (duree_fiche or "").strip()
    # ligne « Durée » sous la date : seulement si le champ est rempli (contrôle, DS…)
    ligne_duree = (r" \\ \textbf{Durée : } " + echapper_latex(duree)) if duree else ""
    # barème : points par exercice (facultatif) ; le total est affiché dans l'en-tête
    pts = nettoyer_points(points, trouves)
    total = sum(pts[i] for i in ids if i in pts)
    ligne_bareme = (r" \\ \textbf{Barème : } " + formater_points(total)) if total > 0 else ""

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
    \textbf{Date : } """ + date_du_jour + ligne_duree + ligne_bareme + r"""
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
\textbf{Nom : }\dotfill\qquad\textbf{Prénom : }\dotfill\mbox{}

\vspace{0.4cm}
"""
    for identifiant in ids:
        if identifiant not in trouves:
            continue
        titre, niveau, enonce = trouves[identifiant]
        # barème : « Exercice 1 (4 points) – Titre » (argument facultatif de l'environnement)
        points_exo = ""
        if identifiant in pts:
            points_exo = "[ (" + formater_points(pts[identifiant]) + ")]"
        code += "\\begin{exercice}" + points_exo + "{" + titre_vers_latex(titre) + " (" + echapper_latex(niveau) + ")}\n"
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
