@echo off
echo shell=cmd
echo cwd=%CD%
echo PYRUNS_EXAMPLE_ENV=%PYRUNS_EXAMPLE_ENV%
python -c "import os; print('python_env_marker=' + os.environ.get('PYRUNS_EXAMPLE_ENV', ''))"
