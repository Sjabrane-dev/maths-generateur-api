# Fichier : API_RENDER/generateur_univ.py
"""Générateur universitaire : feuilles d'exercices tirées de la banque Exo7 (classes prépa, M1-M2).

Routes :
  GET  /univ/chapitres                 -> chapitres (année, filières, nombre d'exercices)
  GET  /univ/exercices/{chapitre}      -> exercices d'un chapitre (énoncé, indication, correction)
  POST /univ/generer-pdf               -> PDF (énoncés, indications, corrigés au choix)

Les exercices viennent d'Exo7 (exo7.emath.fr), sous licence Creative Commons BY-NC-SA :
chaque feuille cite la source, l'identifiant et l'auteur de chaque exercice.
"""
import json
import os
import shutil
import subprocess
import tempfile

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

router = APIRouter(prefix="/univ")

ICI = os.path.dirname(os.path.abspath(__file__))
CHEMIN_BANQUE = os.path.join(ICI, "banque_univ.json")
MAX_EXERCICES = 30
DELAI_COMPILATION = 90

with open(CHEMIN_BANQUE, encoding="utf-8") as f:
    _DONNEES = json.load(f)
CHAPITRES = _DONNEES["chapitres"]                     # liste ordonnée
EXERCICES = {e["id"]: e for e in _DONNEES["exercices"]}
PAR_CHAPITRE = {}
for e in _DONNEES["exercices"]:
    PAR_CHAPITRE.setdefault(e["chapitre"], []).append(e)


def echapper(texte):
    return "".join(
        {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
         "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}.get(c, c)
        for c in (texte or ""))


PREAMBULE = r"""\documentclass[11pt,a4paper]{article}
\usepackage[utf8]{inputenc}
\usepackage[T1]{fontenc}
\usepackage{lmodern}
\usepackage[french]{babel}
\usepackage{amsmath,amsfonts,amssymb}
\usepackage{geometry,fancyhdr,xcolor,array,fancybox,tikz}
\geometry{hmargin=2cm,vmargin=2.3cm}
\definecolor{col1}{RGB}{12,102,98}
\definecolor{col2}{RGB}{248,193,12}
% ----- commandes de la banque Exo7
\newcommand{\Nn}{\mathbb{N}}\newcommand{\Zz}{\mathbb{Z}}\newcommand{\Qq}{\mathbb{Q}}
\newcommand{\Rr}{\mathbb{R}}\newcommand{\Cc}{\mathbb{C}}\newcommand{\Kk}{\mathbb{K}}
\renewcommand{\epsilon}{\varepsilon}
\renewcommand{\Re}{\mathop{\mathrm{Re}}\nolimits}\renewcommand{\Im}{\mathop{\mathrm{Im}}\nolimits}
\renewcommand{\ge}{\geqslant}\renewcommand{\geq}{\geqslant}\renewcommand{\le}{\leqslant}\renewcommand{\leq}{\leqslant}
\newcommand{\dd}{\mathrm{d}}
\newcommand{\ch}{\mathop{\mathrm{ch}}\nolimits}\newcommand{\sh}{\mathop{\mathrm{sh}}\nolimits}
\renewcommand{\tanh}{\mathop{\mathrm{th}}\nolimits}\newcommand{\cotan}{\mathop{\mathrm{cotan}}\nolimits}
\newcommand{\Argsh}{\mathop{\mathrm{argsh}}\nolimits}\newcommand{\Argch}{\mathop{\mathrm{argch}}\nolimits}
\newcommand{\Argth}{\mathop{\mathrm{argth}}\nolimits}\newcommand{\pgcd}{\mathop{\mathrm{pgcd}}\nolimits}
\providecommand{\llbracket}{[\![}\providecommand{\rrbracket}{]\!]}
\setlength{\parindent}{0pt}\setlength{\parskip}{3pt}
% ----- présentation
\pagestyle{fancy}\fancyhf{}
\fancyhead[L]{\small\textcolor{col1}{\textbf{Mathématiques -- Supérieur}}}
\fancyhead[R]{\small\textcolor{col1}{\textbf{@@TITRE@@}}}
\fancyfoot[L]{\scriptsize Exercices Exo7 (exo7.emath.fr) -- licence Creative Commons BY-NC-SA}
\fancyfoot[R]{\small \thepage}
\renewcommand{\headrulewidth}{0.6pt}\renewcommand{\footrulewidth}{0.3pt}
\fancypagestyle{premiere}{\fancyhead{}\renewcommand{\headrulewidth}{0pt}}
\newcounter{numexo}
\newcommand{\Exo}[3]{\par\bigskip\refstepcounter{numexo}\noindent{\color{col1}\large\bfseries Exercice \thenumexo}{\color{col1}#3}\hfill{\scriptsize\color{gray}#1 -- #2}\par\nopagebreak\smallskip}
\newcommand{\Rubrique}[1]{\par\bigskip\noindent{\color{col1}\rule{\linewidth}{0.8pt}}\par\noindent{\color{col1}\Large\bfseries #1}\par\noindent{\color{col1}\rule{\linewidth}{0.8pt}}\par}
\newcommand{\Bloc}[2]{\par\medskip\refstepcounter{numexo}\noindent{\color{col1}\bfseries #1 \thenumexo}\hfill{\scriptsize\color{gray}#2}\par\nopagebreak\smallskip}
"""


def construire_tex(ids, titre, sous_titre, corrections, indications):
    exos = [EXERCICES[i] for i in ids]
    t = [PREAMBULE.replace("@@TITRE@@", echapper(titre)), r"\begin{document}", r"\thispagestyle{premiere}"]
    t.append(r"\begin{center}{\color{col1}\LARGE\bfseries " + echapper(titre) + r"}\\[4pt]")
    if sous_titre:
        t.append(r"{\large " + echapper(sous_titre) + r"}\\[2pt]")
    t.append(r"{\color{col2}\rule{6cm}{2pt}}\end{center}")

    def rubrique(cle, nom):
        t.append(r"\setcounter{numexo}{0}")
        for e in exos:
            if not e.get(cle):
                if cle == "correction":
                    t.append(r"\Bloc{%s}{Exo7 n\textsuperscript{o}\,%s}" % (nom, e["id"].split("-")[1]))
                    t.append(r"\textit{Pas de corrigé disponible dans la banque Exo7 pour cet exercice.}")
                else:
                    t.append(r"\refstepcounter{numexo}")
                continue
            t.append(r"\Bloc{%s}{Exo7 n\textsuperscript{o}\,%s}" % (nom, e["id"].split("-")[1]))
            t.append(e[cle])

    if corrections != "seuls":
        t.append(r"\setcounter{numexo}{0}")
        for e in exos:
            entete = ""
            if e.get("titre"):
                entete += r" -- \textit{" + e["titre"] + "}"
            if e.get("difficulte"):
                entete += r"\quad$" + r"\star" * e["difficulte"] + "$"
            t.append(r"\Exo{Exo7 n\textsuperscript{o}\,%s}{%s}{%s}" % (e["id"].split("-")[1], echapper(e["auteur"]), entete))
            t.append(e["enonce"])
    if indications and any(e.get("indication") for e in exos) and corrections != "seuls":
        t.append(r"\clearpage\Rubrique{Indications}")
        rubrique("indication", "Indication pour l'exercice")
    if corrections in ("fin", "seuls"):
        t.append(r"\clearpage\Rubrique{Corrigés}" if corrections == "fin" else r"\Rubrique{Corrigés}")
        rubrique("correction", "Corrigé de l'exercice")
    t.append(r"\end{document}")
    return "\n".join(t) + "\n"


def compiler(tex):
    dossier = tempfile.mkdtemp(prefix="univ_")
    with open(os.path.join(dossier, "fiche.tex"), "w", encoding="utf-8") as f:
        f.write(tex)
    try:
        for _ in range(2):
            subprocess.run(["pdflatex", "-interaction=nonstopmode", "fiche.tex"], cwd=dossier,
                           capture_output=True, timeout=DELAI_COMPILATION)
    except subprocess.TimeoutExpired:
        shutil.rmtree(dossier, ignore_errors=True)
        raise HTTPException(status_code=500, detail="La compilation a pris trop de temps.")
    pdf = os.path.join(dossier, "fiche.pdf")
    if not os.path.exists(pdf):
        shutil.rmtree(dossier, ignore_errors=True)
        raise HTTPException(status_code=500, detail="La compilation LaTeX a échoué.")
    return pdf, dossier


class DemandeUniv(BaseModel):
    exercices: list[str] = Field(min_length=1, max_length=MAX_EXERCICES)
    titre: str | None = Field(default="Feuille d'exercices", max_length=120)
    sous_titre: str | None = Field(default="", max_length=120)     # ex. « MPSI -- Réduction »
    corrections: str | None = Field(default="sans", max_length=10)  # "sans", "fin" ou "seuls"
    indications: bool = False


@router.get("/chapitres")
def univ_chapitres():
    return [dict(c, nombre=len(PAR_CHAPITRE.get(c["cle"], [])),
                 corriges=sum(1 for e in PAR_CHAPITRE.get(c["cle"], []) if e.get("correction")))
            for c in CHAPITRES]


@router.get("/exercices/{chapitre}")
def univ_exercices(chapitre: str):
    if chapitre not in PAR_CHAPITRE:
        raise HTTPException(status_code=404, detail="Chapitre inconnu.")
    return PAR_CHAPITRE[chapitre]


@router.post("/generer-pdf")
def univ_generer_pdf(demande: DemandeUniv):
    inconnus = [i for i in demande.exercices if i not in EXERCICES]
    if inconnus:
        raise HTTPException(status_code=400, detail="Exercice inconnu : " + inconnus[0])
    mode = demande.corrections if demande.corrections in ("sans", "fin", "seuls") else "sans"
    tex = construire_tex(list(dict.fromkeys(demande.exercices)), demande.titre or "Feuille d'exercices",
                         demande.sous_titre or "", mode, demande.indications)
    pdf, dossier = compiler(tex)
    return FileResponse(pdf, media_type="application/pdf", filename="feuille_universitaire.pdf",
                        background=BackgroundTask(shutil.rmtree, dossier, ignore_errors=True))
