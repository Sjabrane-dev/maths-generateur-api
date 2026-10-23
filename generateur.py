# Fichier : generateur.py
import os
import subprocess
import sqlite3
import glob
from datetime import datetime

PREAMBULE = r"""\documentclass[11pt,a4paper]{article}
\usepackage[utf8]{inputenc}
\usepackage[T1]{fontenc}
\usepackage[french]{babel}
\usepackage{amsmath,amsfonts,amssymb,geometry}
\geometry{hmargin=2.5cm,vmargin=2.5cm}

\newcommand{\N}{\mathbb{N}}
\newcommand{\Z}{\mathbb{Z}}
\newcommand{\Q}{\mathbb{Q}}
\newcommand{\R}{\mathbb{R}}
\newcommand{\D}{\mathbb{D}}
\newcommand{\C}{\mathbb{C}}

\newcounter{exocount}
\newenvironment{exercice}[1]{
    \stepcounter{exocount}
    \vspace{0.6cm}
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

def recuperer_exercice(id_exo):
    """Va chercher un exercice spécifique dans la base de données SQL."""
    conn = sqlite3.connect("banque.db")
    cursor = conn.cursor()
    cursor.execute("SELECT titre, niveau, enonce FROM exercices WHERE id = ?", (id_exo,))
    resultat = cursor.fetchone()
    conn.close()
    return resultat

def assembler_et_compiler(liste_choix, titre_personnalise="Feuille d'exercices", classe_personnalisee="", etablissement=""):
    # 1. Échappement des caractères spéciaux LaTeX
    titre_propre = titre_personnalise.replace('&', '\\&').replace('%', '\\%').replace('#', '\\#').replace('_', '\\_')
    classe_propre = classe_personnalisee.replace('&', '\\&').replace('%', '\\%').replace('#', '\\#').replace('_', '\\_')
    etab_propre = etablissement.replace('&', '\\&').replace('%', '\\%').replace('#', '\\#').replace('_', '\\_')
    
    date_du_jour = datetime.now().strftime("%d/%m/%Y")
    
    # 2. Construction de l'en-tête
    entete_latex = ""
    
    # Établissement en haut à droite (seulement s'il est rempli)
    if etab_propre:
        entete_latex += r"\begin{flushright}\textsc{" + etab_propre + r"}\end{flushright}" + "\n"

    # Titre, Date et Classe au centre
    entete_latex += r"""
\begin{center}
    {\LARGE \textbf{""" + titre_propre + r"""}} \\[0.4cm]
    {\large """ + date_du_jour + ((" -- " + classe_propre) if classe_propre else "") + r"""} \\[0.5cm]
\end{center}
\hrule
\vspace{0.5cm}
"""
    
    code_final = PREAMBULE + entete_latex

    # 3. Ajout des exercices
    for identifiant in liste_choix:
        donnees_exo = recuperer_exercice(identifiant)
        if donnees_exo:
            titre, niveau, enonce = donnees_exo
            code_final += f"\\begin{{exercice}}{{{titre} ({niveau})}}\n"
            code_final += enonce + "\n"
            code_final += "\\end{exercice}\n"
        else:
            print(f"⚠️ L'exercice {identifiant} n'existe pas dans la base SQL.")

    code_final += FOOTER

    # 4. Écriture du fichier .tex et compilation
    nom_fichier_tex = "feuille_exercices.tex"
    with open(nom_fichier_tex, "w", encoding="utf-8") as f:
        f.write(code_final)

    print("\n⏳ Compilation du PDF en cours...")

    resultat = subprocess.run(
        ["pdflatex", "-interaction=nonstopmode", nom_fichier_tex],
        capture_output=True,
        text=True
    )

    if resultat.returncode != 0:
        print("❌ ERREUR LATEX :")
        print(resultat.stdout)
        print(resultat.stderr)
        nettoyer_fichiers_temporaires()
        raise Exception("La compilation LaTeX a échoué")

    nettoyer_fichiers_temporaires()
    print(f"🎉 Succès ! Le fichier 'feuille_exercices.pdf' est prêt.")


def nettoyer_fichiers_temporaires():
    """Supprime tous les fichiers auxiliaires générés par la compilation LaTeX."""
    extensions_cache = ["*.aux", "*.log", "*.toc", "*.synctex.gz", "feuille_exercices.tex"]
    
    for motif in extensions_cache:
        fichiers_trouves = glob.glob(motif)
        for fichier in fichiers_trouves:
            try:
                os.remove(fichier)
            except Exception as e:
                print(f"⚠️ Impossible de supprimer le fichier temporaire {fichier} : {e}")


if __name__ == "__main__":
    print("--- GÉNÉRATEUR D'EXERCICES (VERSION SQL) ---")

    conn = sqlite3.connect("banque.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, titre, niveau FROM exercices")
    print("Exercices disponibles :")
    for row in cursor.fetchall():
        print(f"  - [{row[0]}] {row[1]} ({row[2]})")
    conn.close()

    choix_utilisateur = input("\nTapez les codes des exercices voulus (ex: SEC-001, SEC-002) : ")
    liste_choix = [c.strip() for c in choix_utilisateur.split(",")]

    assembler_et_compiler(liste_choix)