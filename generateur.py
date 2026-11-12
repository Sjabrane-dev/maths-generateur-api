# Fichier : API_RENDER/generateur.py
import hashlib
import os
import random
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


# Corrigés : "sans" (énoncés seuls), "fin" (énoncés puis corrigés), "seuls" (corrigés seuls)
MODES_CORRIGES = ("sans", "fin", "seuls")


TEXTES_CALCULATRICE = {
    "autorisee": "Calculatrice autorisée",
    "interdite": "Calculatrice interdite",
}


class ErreurGeneration(Exception):
    """La compilation LaTeX a échoué (erreur côté serveur)."""


def generer_preambule(titre_fiche="Feuille d'exercices", lycee="LPO Jacques Brel"):
    titre_echappe = "".join(
        {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$",
         "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
         "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}.get(c, c)
        for c in (titre_fiche or "Feuille d'exercices")
    )
    return r"""\documentclass[11pt,a4paper]{article}
\usepackage[utf8]{inputenc}
\usepackage[T1]{fontenc}
\usepackage{lmodern}
\usepackage[french]{babel}
\usepackage{amsmath,amsfonts,amssymb,geometry,fancyhdr}
\usepackage{tikz,multicol,array,eurosym,xcolor,listings}
\usetikzlibrary{calc,angles,quotes,positioning,shapes.geometric,arrows.meta}

% ===== Charte graphique du site =====
\definecolor{col1}{RGB}{12, 102, 98}

\newcommand{\U}{\mathbb{U}}
\newcommand{\K}{\mathbb{K}}
\renewcommand{\Re}{\operatorname{Re}}
\renewcommand{\Im}{\operatorname{Im}}

\tikzset{gr/.style={circle,fill=col1,text=white,minimum size=6.5mm,inner sep=0pt,font=\footnotesize},
  lien/.style={line width=0.6pt},
  fl/.style={->,>=stealth,line width=0.6pt}}

\lstset{language=Python,basicstyle=\ttfamily\small,keepspaces=true,showstringspaces=false,columns=fullflexible,
  literate={é}{{\'e}}1 {è}{{\`e}}1 {à}{{\`a}}1 {ê}{{\^e}}1 {ç}{{\c c}}1}

\geometry{hmargin=2cm,vmargin=2.5cm}

% ===== En-têtes et pieds de page =====
\pagestyle{fancy}
\fancyhf{}
\fancyhead[L]{\small \textcolor{col1}{\textbf{Mathématiques}}}
\fancyhead[R]{\small \textcolor{col1}{\textbf{""" + titre_echappe + r"""}}}
\fancyfoot[L]{\small """ + echapper_latex(lycee) + r"""}
\fancyfoot[R]{\small Page \thepage}
\renewcommand{\headrulewidth}{0.6pt}
\renewcommand{\footrulewidth}{0.4pt}
% Première page : sans en-tête (le titre est déjà en grand), même pied de page
\fancypagestyle{premiere}{\fancyhead{}\renewcommand{\headrulewidth}{0pt}}

\newcommand{\N}{\mathbb{N}}
\newcommand{\Z}{\mathbb{Z}}
\newcommand{\Q}{\mathbb{Q}}
\newcommand{\R}{\mathbb{R}}
\newcommand{\D}{\mathbb{D}}
\newcommand{\C}{\mathbb{C}}

\newcounter{exocount}
\newenvironment{exercice}[2][]{
    \stepcounter{exocount}
    \vspace{0.4cm}
    \noindent{\color{col1}\textbf{Exercice \arabic{exocount}#1}} \textbf{-- #2}
    \par\vspace{0.1cm}
}{
    \par
}

\begin{document}
"""

FOOTER = r"""
\end{document}
"""

# ---------------------------------------------------------------------------
# Protection du LaTeX : tout texte venant de l'extérieur est « échappé »
# ---------------------------------------------------------------------------
_SPECIAUX = {
    "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$",
    "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
    "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
}

_MOT_MATHS = re.compile(r"[A-Za-z0-9]+(?:[_^](?:\{[^{}]*\}|[A-Za-z0-9]+))+")


def echapper_latex(texte):
    return "".join(_SPECIAUX.get(c, c) for c in (texte or ""))


def titre_vers_latex(titre):
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
    if not ids:
        return {}
    marques = ",".join("?" * len(ids))
    with closing(sqlite3.connect(CHEMIN_BASE)) as conn:
        lignes = conn.execute(
            f"SELECT id, titre, niveau, enonce, correction FROM exercices WHERE id IN ({marques})",
            ids,
        ).fetchall()
    return {l[0]: (l[1], l[2], l[3], l[4] or "") for l in lignes}


def tirer_version_b(ids_a, variante=0):
    """Pour chaque exercice du sujet A, choisit un autre exercice de la banque
    du même niveau, du même chapitre et de la même difficulté (sinon d'une difficulté
    voisine, sinon du même chapitre). Le tirage est reproductible : mêmes exercices A
    et même numéro de variante donnent le même sujet B.
    Renvoie (liste_b, nb_sans_jumeau)."""
    if not ids_a:
        return [], 0
    graine = hashlib.sha256((",".join(ids_a) + "|" + str(int(variante or 0))).encode("utf-8")).hexdigest()
    hasard = random.Random(int(graine[:16], 16))
    with closing(sqlite3.connect(CHEMIN_BASE)) as conn:
        infos = {l[0]: (l[1], l[2], l[3]) for l in conn.execute(
            f"SELECT id, niveau, chapitre, difficulte FROM exercices WHERE id IN ({','.join('?' * len(ids_a))})",
            ids_a).fetchall()}
        groupes = {}
        for niveau, chapitre in {(v[0], v[1]) for v in infos.values()}:
            groupes[(niveau, chapitre)] = conn.execute(
                "SELECT id, difficulte FROM exercices WHERE niveau = ? AND chapitre = ? ORDER BY id",
                (niveau, chapitre)).fetchall()
    pris = set(ids_a)
    liste_b, sans = [], 0
    for ident in ids_a:
        if ident not in infos:
            continue
        niveau, chapitre, diff = infos[ident]
        candidats = [(i, d) for i, d in groupes.get((niveau, chapitre), []) if i not in pris]
        choix = None
        if candidats:
            ecart_min = min(abs((d or 0) - (diff or 0)) for _, d in candidats)
            meilleurs = [i for i, d in candidats if abs((d or 0) - (diff or 0)) == ecart_min]
            choix = hasard.choice(meilleurs)
        if choix is None:
            choix, sans = ident, sans + 1   # aucun autre exercice dans ce chapitre : on garde le même
        pris.add(choix)
        liste_b.append(choix)
    return liste_b, sans


def formater_points(x):
    nombre = f"{x:g}".replace(".", ",")
    return f"{nombre}~point" + ("s" if x > 1 else "")


def nettoyer_points(points, trouves):
    propres = {}
    for cle, valeur in (points or {}).items():
        try:
            x = float(valeur)
        except (TypeError, ValueError):
            continue
        if cle in trouves and 0 < x <= 100:
            propres[cle] = x
    return propres


def generer_page_presentation(titre_fiche, lycee, niveau_affiche, date_du_jour,
                              duree="", total=0, nb_exercices=0, citation="",
                              auteur_citation="", calculatrice=""):
    """Page de garde dans le style des chapitres (couleur col1, cadre, citation)."""
    lignes_infos = [r"\textbf{Date :} " + date_du_jour]
    if duree:
        lignes_infos.append(r"\textbf{Durée :} " + echapper_latex(duree))
    if total > 0:
        lignes_infos.append(r"\textbf{Barème :} " + formater_points(total))
    if nb_exercices:
        lignes_infos.append(r"\textbf{Nombre d'exercices :} " + str(nb_exercices))
    if calculatrice:
        lignes_infos.append(r"\textbf{" + echapper_latex(calculatrice) + "}")
    infos = r" \\[2mm] ".join(lignes_infos)

    bloc_citation = ""
    if citation:
        signature = ""
        if auteur_citation:
            signature = r"\par\vspace{2mm}\hfill--- \textsc{" + echapper_latex(auteur_citation) + "}"
        bloc_citation = r"""
\vspace{1.2cm}
\begin{center}
\begin{minipage}{0.75\textwidth}
\color{col1!80!black}\itshape\large
\og """ + echapper_latex(citation) + r""" \fg""" + signature + r"""
\end{minipage}
\end{center}
"""

    return r"""
\begin{titlepage}
\begin{tikzpicture}[remember picture,overlay]
  % bandeaux de couleur haut et bas
  \fill[col1] (current page.north west) rectangle ([yshift=-2.2cm]current page.north east);
  \fill[col1] (current page.south west) rectangle ([yshift=1.4cm]current page.south east);
  % cadre fin
  \draw[col1,line width=1.2pt] ([xshift=1.2cm,yshift=-2.8cm]current page.north west)
        rectangle ([xshift=-1.2cm,yshift=2cm]current page.south east);
  % texte des bandeaux
  \node[text=white,font=\Large\bfseries,anchor=west] at ([xshift=1.5cm,yshift=-1.1cm]current page.north west)
        {""" + echapper_latex(lycee) + r"""};
  \node[text=white,font=\large,anchor=east] at ([xshift=-1.5cm,yshift=-1.1cm]current page.north east)
        {Année scolaire """ + annee_scolaire() + r"""};
  \node[text=white,font=\small] at ([yshift=0.7cm]current page.south)
        {Mathématiques};
  % motif décoratif (logo provisoire)
  \begin{scope}[shift={([yshift=-6.3cm]current page.north)}]
    \draw[col1,line width=1pt] (0,0) circle (1.3);
    \draw[col1!60,line width=0.8pt] (0,0) circle (1.0);
    \draw[col1,line width=1.2pt,domain=-0.9:0.9,smooth,samples=40] plot (\x,{0.9*sin(200*\x)});
    \draw[col1!60,->,>=stealth] (-1.1,0) -- (1.1,0);
    \draw[col1!60,->,>=stealth] (0,-1.1) -- (0,1.1);
  \end{scope}
\end{tikzpicture}

\vspace*{6.2cm}
\begin{center}
  {\Large\color{col1!80!black}\textsc{""" + echapper_latex(niveau_affiche) + r"""}}\\[0.8cm]
  {\color{col1}\rule{0.6\textwidth}{1.5pt}}\\[0.6cm]
  {\fontsize{30}{36}\selectfont\bfseries\color{col1} """ + echapper_latex(titre_fiche) + r"""}\\[0.6cm]
  {\color{col1}\rule{0.6\textwidth}{1.5pt}}
\end{center}
""" + bloc_citation + r"""
\vfill
\begin{center}
\begin{tikzpicture}
  \node[draw=col1,line width=1pt,rounded corners=6pt,fill=col1!7,inner sep=12pt,align=left,text width=9cm]
  {""" + infos + r"""};
\end{tikzpicture}
\end{center}

\vspace*{2cm}
\end{titlepage}
"""


# ---------------------------------------------------------------------------
# Page de présentation « Comme mes chapitres » (style perso, version épurée)
# ---------------------------------------------------------------------------
MACROS_CHAPITRE = r"""
\definecolor{col2}{RGB}{248, 193, 12}
% Ornements : pgfornament s'il est installé, sinon un simple losange
\IfFileExists{pgfornament.sty}{\usepackage{pgfornament}\def\ornok{1}}{}
\providecommand{\orn}[3]{\ifdefined\ornok\pgfornament[width=#2,color=#3]{#1}\else\textcolor{#3}{$\blacklozenge$}\fi}
% Logo avec le niveau écrit sur la bande (comme logoVincent)
\providecommand{\logoNiveau}[3][1]{%
  \begin{tikzpicture}[scale=#1, very thick]
    \def\arete{2.0}\def\epaisseur{0.9}\def\rayon{0.4}
    \def\ruban{(0,0) ++(0:0.57735*\arete-0.57735*\epaisseur+2*\rayon)
      ++(-30:\epaisseur-1.73205*\rayon)
      arc (60:0:\rayon) -- ++(90:\epaisseur)
      arc (0:60:\rayon) -- ++(150:\arete)
      arc (60:120:\rayon) -- ++(210:\epaisseur)
      arc (120:60:\rayon) -- cycle}
    \shade[draw=col2!50, top color=col1!80!white, bottom color=col1!60!black] \ruban;
    \shade[draw=col2!50, top color=col1!80!white, bottom color=col1!60!black, rotate=120] \ruban;
    \shade[draw=col2!50, top color=col1!80!white, bottom color=col1!60!black, rotate=-120] \ruban;
    \draw[col2, line width=0.4pt, opacity=0.7] \ruban;
    \draw[col2, line width=0.4pt, opacity=0.7, rotate=120] \ruban;
    \draw[col2, line width=0.4pt, opacity=0.7, rotate=-120] \ruban;
    \node at (172:1.1) [rotate=90, color=col2, font=\bfseries#3\selectfont, inner sep=0pt] {#2};
    \node at (-42:1.2) [rotate=30, color=col2, font=\itshape\fontsize{9}{11}\selectfont, inner sep=0pt] {Math\;Brel};
    \begin{scope}[scale=0.42]
      \draw[col2, line width=0.6pt] (0,0.9) -- (-0.78,-0.45) -- (0.78,-0.45) -- cycle;
      \draw[col2, line width=0.45pt] (0,-0.15) -- (-0.39,-0.45) -- (0.39,-0.45) -- cycle;
      \draw[col2, line width=0.3pt] (0,0.4) -- (-0.2,0.1) -- (0.2,0.1) -- cycle;
      \draw[col2, line width=0.3pt] (-0.4,-0.05) -- (-0.6,-0.35) -- (-0.2,-0.35) -- cycle;
      \draw[col2, line width=0.3pt] (0.4,-0.05) -- (0.2,-0.35) -- (0.6,-0.35) -- cycle;
    \end{scope}
  \end{tikzpicture}%
}
% Logo bande de Möbius
\providecommand{\logoSaid}[1][1]{%
  \begin{tikzpicture}[scale=#1, very thick]
    \def\arete{2.0}\def\epaisseur{0.9}\def\rayon{0.4}
    \def\ruban{(0,0) ++(0:0.57735*\arete-0.57735*\epaisseur+2*\rayon)
      ++(-30:\epaisseur-1.73205*\rayon)
      arc (60:0:\rayon) -- ++(90:\epaisseur)
      arc (0:60:\rayon) -- ++(150:\arete)
      arc (60:120:\rayon) -- ++(210:\epaisseur)
      arc (120:60:\rayon) -- cycle}
    \shade[draw=col2!50, top color=col1!80!white, bottom color=col1!60!black] \ruban;
    \shade[draw=col2!50, top color=col1!80!white, bottom color=col1!60!black, rotate=120] \ruban;
    \shade[draw=col2!50, top color=col1!80!white, bottom color=col1!60!black, rotate=-120] \ruban;
    \draw[col2, line width=0.4pt, opacity=0.7] \ruban;
    \draw[col2, line width=0.4pt, opacity=0.7, rotate=120] \ruban;
    \draw[col2, line width=0.4pt, opacity=0.7, rotate=-120] \ruban;
    \begin{scope}[scale=0.42]
      \draw[col2, line width=0.6pt] (0,0.9) -- (-0.78,-0.45) -- (0.78,-0.45) -- cycle;
      \draw[col2, line width=0.45pt] (0,-0.15) -- (-0.39,-0.45) -- (0.39,-0.45) -- cycle;
      \draw[col2, line width=0.3pt] (0,0.4) -- (-0.2,0.1) -- (0.2,0.1) -- cycle;
      \draw[col2, line width=0.3pt] (-0.4,-0.05) -- (-0.6,-0.35) -- (-0.2,-0.35) -- cycle;
      \draw[col2, line width=0.3pt] (0.4,-0.05) -- (0.2,-0.35) -- (0.6,-0.35) -- cycle;
    \end{scope}
  \end{tikzpicture}%
}
"""


def generer_page_chapitre(titre_fiche, lycee, niveau_affiche, date_du_jour,
                          duree="", total=0, nb_exercices=0, citation="",
                          auteur_citation="", calculatrice=""):
    """Page de garde dans le style des chapitres (bandeau, ornements, logo Möbius)."""
    ligne_duree = ""
    if duree:
        ligne_duree = r"\\[6pt]{\LARGE\color{col1}\scshape Durée : " + echapper_latex(duree) + "}"
    ligne_calc = ""
    if calculatrice:
        ligne_calc = r"{\large\color{col1}\itshape " + echapper_latex(calculatrice) + r"}\par\vspace{0.6\baselineskip}"
    # niveau en capitales (bande latérale et logo)
    niveau_maj = echapper_latex((niveau_affiche or "").upper())
    # sur le logo : le niveau sans le numéro de groupe (« Seconde 7 » -> « SECONDE »)
    niveau_logo = re.sub(r"\s*\d+\s*$", "", (niveau_affiche or "").strip()).upper()
    # taille du texte adaptée à sa longueur pour rester sur la bande
    t = min(12, round(62 / max(len(niveau_logo), 1), 1))
    taille_logo = r"\fontsize{" + str(t) + "}{" + str(t + 2) + "}"
    niveau_logo = echapper_latex(niveau_logo)

    infos = [r"Date : " + date_du_jour]
    if total > 0:
        infos.append(r"Barème : " + formater_points(total))
    if nb_exercices:
        infos.append(str(nb_exercices) + " exercice" + ("s" if nb_exercices > 1 else ""))
    ligne_infos = r"\quad\textcolor{col2}{$\bullet$}\quad ".join(infos)

    bloc_citation = ""
    if citation:
        signature = ""
        if auteur_citation:
            signature = r"\\[3pt]{\small\color{col1}--- \textsc{" + echapper_latex(auteur_citation) + "}}"
        bloc_citation = (r"{\large\color{col1}\itshape \og " + echapper_latex(citation)
                         + r" \fg}" + signature)

    return r"""
\begin{titlepage}
\begin{tikzpicture}[remember picture,overlay]
  \fill[col1] (current page.north west) rectangle ([xshift=1cm]current page.south west);
  \node[white,rotate=90,anchor=east] at ([xshift=0.5cm,yshift=-2.5cm]current page.north west)
       {\Large\bfseries\sffamily """ + niveau_maj + r"""};
  \node[white,rotate=90] at ([xshift=0.5cm,yshift=4.7cm]current page.south west)
       {\Large\bfseries\sffamily LPO};
\end{tikzpicture}%
\centering
\vspace*{\baselineskip}

\noindent{\color{col2}\rule{\textwidth}{2.5pt}}\\[6pt]
\noindent\colorbox{col1}{%
  \parbox[c][1.4cm][c]{\dimexpr\textwidth-2\fboxsep\relax}{%
    \centering\color{white}\Large\scshape
    \orn{97}{1cm}{white}\quad """ + echapper_latex(niveau_affiche) + r"""\quad\orn{98}{1cm}{white}
  }%
}\\[6pt]
\noindent{\color{col2}\rule{\textwidth}{2.5pt}}

\vspace{1.2\baselineskip}
{\large\color{col1}\orn{88}{0.6cm}{col1}\quad\scshape Mathématiques\quad\orn{88}{0.6cm}{col1}}

\vspace{2.5\baselineskip}
{\Huge\bfseries\color{col1}\scshape """ + echapper_latex(titre_fiche) + r"""}""" + ligne_duree + r"""

\vspace{0.8\baselineskip}
{\color{col2}\rule{0.35\textwidth}{1.5pt}}
\vspace{0.8\baselineskip}

""" + ligne_calc + bloc_citation + r"""

\vfill
{\color{col1}""" + ligne_infos + r"""}

\vspace{\baselineskip}
\orn{75}{4.5cm}{col1}

\vfill
{\large\color{col1}\scshape """ + echapper_latex(lycee) + r"""}

\vspace{0.8\baselineskip}
\logoNiveau[0.9]{""" + niveau_logo + r"""}{""" + taille_logo + r"""}

\vspace{0.8\baselineskip}
\colorbox{col1}{\parbox{3.6cm}{\centering\color{white}\bfseries Année """ + annee_scolaire().replace("-", "/") + r"""}}
\end{titlepage}
"""


def assembler_et_compiler(liste_choix, titre_fiche="Feuille d'exercices",
                          classe_fiche="", etablissement_fiche="", duree_fiche="",
                          points=None, page_presentation=False,
                          citation="", auteur_citation="",
                          style_presentation="moderne", calculatrice="",
                          corrections="sans", versions="A", variante=0):
    corrections = corrections if corrections in MODES_CORRIGES else "sans"
    deux_versions = (versions or "A").upper() == "AB"
    ids = list(dict.fromkeys(liste_choix))
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

    ids_b = []
    if deux_versions:
        ids_b, _ = tirer_version_b([i for i in ids if i in trouves], variante)
        trouves.update(recuperer_exercices([i for i in ids_b if i not in trouves]))

    lycee = (etablissement_fiche or "").strip() or "LPO Jacques Brel"
    
    if classe_fiche and classe_fiche.strip():
        niveau_affiche = classe_fiche.strip()
    else:
        niveaux = list(dict.fromkeys(trouves[i][1] for i in ids if i in trouves))
        niveau_affiche = " / ".join(niveaux)
        
    titre_fiche = (titre_fiche or "").strip() or "Feuille d'exercices"
    if corrections == "seuls":
        titre_fiche += " — Corrigé"
    date_du_jour = datetime.now().strftime("%d/%m/%Y")
    duree = (duree_fiche or "").strip()
    texte_calc = TEXTES_CALCULATRICE.get((calculatrice or "").strip(), "")
    ligne_duree = (r"\\[2mm] {\large Durée : " + echapper_latex(duree) + "}") if duree else ""
    if texte_calc:
        ligne_duree += r"\\[1mm] {\itshape " + texte_calc + "}"
    pts = nettoyer_points(points, trouves)
    total = sum(pts[i] for i in ids if i in pts)
    presents = [i for i in ids if i in trouves]
    pts_b = {b_: pts[a_] for a_, b_ in zip(presents, ids_b) if a_ in pts}
    ligne_bareme = (r" \\ \textbf{Barème : } " + formater_points(total)) if total > 0 else ""

    # Utilisation de la fonction dynamique pour générer le préambule avec le bon titre en haut
    code = generer_preambule(titre_fiche, lycee)
    if page_presentation and style_presentation == "chapitre":
        code = code.replace("\\begin{document}", MACROS_CHAPITRE + "\n\\begin{document}", 1)
    
    def entete(titre_fiche):
      code = ""
      if page_presentation:
        # Styles : "moderne" (bandeaux verts) ou "chapitre" (comme mes chapitres)
        fabriques = {"moderne": generer_page_presentation,
                     "chapitre": generer_page_chapitre}
        fabrique = fabriques.get(style_presentation, generer_page_presentation)
        code += fabrique(
            titre_fiche, lycee, niveau_affiche, date_du_jour, duree, total,
            len([i for i in ids if i in trouves]),
            (citation or "").strip(), (auteur_citation or "").strip(),
            texte_calc,
        )
      else:
        code += r"""
\thispagestyle{premiere}
\noindent
\begin{minipage}[t]{0.5\textwidth}
    \textbf{""" + echapper_latex(lycee) + r"""} \\
    Année scolaire """ + annee_scolaire() + r"""
\end{minipage}
\begin{minipage}[t]{0.5\textwidth}
    \raggedleft
    \textbf{""" + echapper_latex(niveau_affiche) + r"""} \\
    \textbf{Date : } """ + date_du_jour + ligne_bareme + r"""
\end{minipage}

\vspace{0.3cm}
\noindent{\color{col1}\rule{\textwidth}{0.8pt}}
\vspace{0.2cm}

\begin{center}
    {\LARGE \bfseries \color{col1} """ + echapper_latex(titre_fiche) + r"""}""" + ligne_duree + r"""
\end{center}

\vspace{0.1cm}
\noindent{\color{col1}\rule{\textwidth}{1.5pt}}
\vspace{0.4cm}
""" + ("" if corrections == "seuls" else r"""
\noindent
\textbf{Nom : }\dotfill\qquad\textbf{Prénom : }\dotfill\mbox{}

\vspace{0.4cm}
""")
      return code

    if not deux_versions:
        code += entete(titre_fiche)
        if corrections != "seuls":
            code += _blocs_enonces(presents, trouves, pts)
        if corrections == "fin":
            code += _titre_corriges("Corrigés")
        if corrections in ("fin", "seuls"):
            code += _blocs_corriges(presents, trouves)
    else:
        versions_ab = [("A", presents, pts), ("B", ids_b, pts_b)]
        if corrections != "seuls":
            for k, (lettre, liste, p) in enumerate(versions_ab):
                if k:
                    code += "\n\\newpage\n\\setcounter{page}{1}\n"
                code += "\\setcounter{exocount}{0}\n" + entete(titre_fiche + " — Sujet " + lettre)
                code += _blocs_enonces(liste, trouves, p)
        if corrections in ("fin", "seuls"):
            for k, (lettre, liste, p) in enumerate(versions_ab):
                if corrections == "seuls" and k == 0:
                    code += entete(titre_fiche + " — Sujet " + lettre)
                elif corrections == "seuls":
                    code += "\n\\newpage\n" + entete(titre_fiche + " — Sujet " + lettre)
                else:
                    code += _titre_corriges("Corrigés — Sujet " + lettre)
                code += _blocs_corriges(liste, trouves)
    code += FOOTER

    # La page de présentation utilise des positions absolues : 2 passes nécessaires
    return _compiler(code, passes=2 if page_presentation else 1)


def _titre_corriges(texte):
    return r"""
\newpage
\begin{center}
    {\LARGE \bfseries \color{col1} """ + echapper_latex(texte) + r"""}
\end{center}
\noindent{\color{col1}\rule{\textwidth}{1.5pt}}
\vspace{0.2cm}
"""


def _blocs_enonces(presents, trouves, pts):
    code = ""
    for identifiant in presents:
        titre, niveau, enonce, _ = trouves[identifiant]
        points_exo = ""
        if identifiant in pts:
            points_exo = "[ (" + formater_points(pts[identifiant]) + ")]"
        code += "\\begin{exercice}" + points_exo + "{" + titre_vers_latex(titre) + " (" + echapper_latex(niveau) + ")}\n"
        code += enonce + "\n"
        code += "\\end{exercice}\n"
    return code


def _blocs_corriges(presents, trouves):
    """Corrigés numérotés comme les exercices de la feuille (Exercice 1, 2…)."""
    code = ""
    for numero, identifiant in enumerate(presents, 1):
        titre, niveau, _, correction = trouves[identifiant]
        code += ("\n\\vspace{0.4cm}\n\\noindent{\\color{col1}\\textbf{Corrigé de l'exercice "
                 + str(numero) + "}} \\textbf{-- " + titre_vers_latex(titre) + "}\n\\par\\vspace{0.1cm}\n")
        code += (correction.strip() or r"\emph{Pas de corrigé disponible pour cet exercice.}") + "\n\\par\n"
    return code


def _compiler(code_tex, passes=1):
    dossier = tempfile.mkdtemp(prefix="fiche_")
    try:
        chemin_tex = os.path.join(dossier, "feuille_exercices.tex")
        chemin_pdf = os.path.join(dossier, "feuille_exercices.pdf")
        with open(chemin_tex, "w", encoding="utf-8") as f:
            f.write(code_tex)

        print("⏳ Compilation du PDF via pdflatex...")
        for _ in range(passes):
            try:
                res = subprocess.run(
                    ["pdflatex", "-interaction=nonstopmode", "-halt-on-error",
                     "-no-shell-escape", "-output-directory", dossier, chemin_tex],
                    capture_output=True, cwd=dossier, timeout=DELAI_COMPILATION,
                )
            except subprocess.TimeoutExpired:
                raise ErreurGeneration("La compilation LaTeX a pris trop de temps.")
            if res.returncode != 0:
                break

        if res.returncode != 0 or not os.path.exists(chemin_pdf):
            sortie = res.stdout.decode("utf-8", errors="replace")
            print("❌ ERREUR LATEX DÉTECTÉE :\n" + sortie[-3000:])
            raise ErreurGeneration("La compilation LaTeX a échoué.")

        print("🎉 Succès ! Le fichier PDF est prêt.")
        return chemin_pdf, dossier
    except Exception:
        shutil.rmtree(dossier, ignore_errors=True)
        raise
