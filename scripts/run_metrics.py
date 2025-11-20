"""Script para ejecutar todas las métricas de calidad de código."""
import subprocess
import sys
from pathlib import Path

# Colores para output
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
RESET = "\033[0m"


def print_header(text: str) -> None:
    """Imprime encabezado destacado."""
    print(f"\n{BLUE}{'=' * 60}{RESET}")
    print(f"{BLUE}{text.center(60)}{RESET}")
    print(f"{BLUE}{'=' * 60}{RESET}\n")


def run_command(command: list[str], description: str) -> tuple[bool, str]:
    """
    Ejecuta un comando y retorna el resultado.

    Args:
        command: Comando a ejecutar
        description: Descripción del comando

    Returns:
        Tupla (éxito, output)
    """
    print(f"{YELLOW}Ejecutando: {description}{RESET}")
    print(f"Comando: {' '.join(command)}\n")

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False
        )

        output = result.stdout + result.stderr
        success = result.returncode == 0

        if success:
            print(f"{GREEN}✓ {description} - Completado{RESET}")
        else:
            print(f"{RED}✗ {description} - Falló{RESET}")

        print(output)
        return success, output

    except FileNotFoundError:
        error_msg = f"{RED}✗ Herramienta no encontrada. Instala con: pip install -r requirements.txt{RESET}"
        print(error_msg)
        return False, error_msg


def main():
    """Ejecuta todas las métricas de calidad."""
    print_header("MÉTRICAS DE CALIDAD DE CÓDIGO")

    results = {}

    # 1. Cobertura de tests
    print_header("1. Cobertura de Tests (Objetivo: >85%)")
    success, output = run_command(
        [
            sys.executable, "-m", "pytest",
            "tests/config/",
            "tests/integration/",
            "--cov=app/config",
            "--cov-report=term-missing",
            "--cov-report=html:htmlcov",
            "-v"
        ],
        "Cobertura de tests"
    )
    results["Cobertura"] = success

    # 2. Complejidad ciclomática
    print_header("2. Complejidad Ciclomática (Objetivo: <10)")
    success, output = run_command(
        [
            "radon", "cc", "app/config/", "-a", "-s"
        ],
        "Complejidad ciclomática"
    )
    results["Complejidad"] = success

    # 3. Mantenibilidad
    print_header("3. Índice de Mantenibilidad (Objetivo: A o B)")
    success, output = run_command(
        ["radon", "mi", "app/config/", "-s"],
        "Índice de mantenibilidad"
    )
    results["Mantenibilidad"] = success

    # 4. Type hints con mypy
    print_header("4. Type Hints con MyPy (Objetivo: 100%)")
    success, output = run_command(
        [
            sys.executable, "-m", "mypy",
            "app/config/",
            "--ignore-missing-imports",
            "--show-error-codes"
        ],
        "Verificación de tipos con MyPy"
    )
    results["Type Hints"] = success

    # 5. Duplicación de código
    print_header("5. Duplicación de Código (Objetivo: <3%)")
    success, output = run_command(
        [
            "pylint",
            "app/config/",
            "--disable=all",
            "--enable=duplicate-code",
            "--min-similarity-lines=4"
        ],
        "Duplicación de código"
    )
    results["Duplicación"] = success

    # Resumen final
    print_header("RESUMEN DE RESULTADOS")

    total = len(results)
    passed = sum(1 for v in results.values() if v)

    for metric, success in results.items():
        status = f"{GREEN}✓ PASS{RESET}" if success else f"{RED}✗ FAIL{RESET}"
        print(f"{metric:20} {status}")

    print(f"\n{BLUE}Total: {passed}/{total} métricas pasaron{RESET}")

    if passed == total:
        print(f"{GREEN}¡Todas las métricas de calidad pasaron!{RESET}")
        return 0
    else:
        print(f"{YELLOW}Algunas métricas necesitan atención{RESET}")
        return 1


if __name__ == "__main__":
    sys.exit(main())