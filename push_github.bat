@echo off
echo ============================================
echo   Lorena Bot -- Push para GitHub
echo ============================================
echo.

cd /d "%~dp0"

IF NOT EXIST ".git" (
    echo Inicializando repositorio git...
    git init
    git branch -M main
)

echo Adicionando arquivos...
git add .

echo Fazendo commit...
git commit -m "feat: Lorena Bot - deploy inicial Railway" 2>nul || git commit --allow-empty -m "feat: Lorena Bot - deploy inicial Railway"

echo Configurando remote...
git remote remove origin 2>nul
git remote add origin https://github.com/tiagoraggi-tech/lorena-bot.git

echo Fazendo push...
git push -u origin main

echo.
echo ============================================
echo   Concluido! Verifique em:
echo   https://github.com/tiagoraggi-tech/lorena-bot
echo ============================================
pause
