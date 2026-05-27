Write-Host "shell=powershell"
Write-Host "pwd=$PWD"
Write-Host "PYRUNS_EXAMPLE_ENV=$env:PYRUNS_EXAMPLE_ENV"

python -c "import os; print('python_env_marker=' + os.environ.get('PYRUNS_EXAMPLE_ENV', ''))"
python -c "import os; print('helloword')"
