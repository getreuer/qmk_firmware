"""Look up QMK keycode numeric value by name, or vice versa.
"""
from pathlib import Path
import os
import re
import shlex
import shutil
import tempfile

from argcomplete.completers import FilesCompleter
from milc import cli

from qmk.decorators import automagic_keyboard, automagic_keymap
from qmk.keyboard import keyboard_completer, keyboard_folder
from qmk.keymap import keymap_completer
from qmk.constants import INTERMEDIATE_OUTPUT_PREFIX
from qmk.path import normpath, FileType


def generate_c_source(keycodes: list, keyboard: str | None, keymap: str | None, extra_headers: list) -> tuple:
    """Generates the C source code and determines include directories.
    """
    # Include just enough to define keycodes, for name to numeric conversion,
    # and for the get_keycode_string() utility, for numeric to name conversion.
    include_dirs = [
        '.',
        'quantum',
        'quantum/keymap_extras',
        'quantum/sequencer',
        'platforms',
    ]
    c_lines = [
        '#include <stdbool.h>',
        '#include <stdint.h>',
        '#include <stdio.h>',
        '#include "quantum/keycode.h"',
        '#include "quantum/keycode_string.h"',
        '#include "quantum/keycodes.h"',
        '#include "quantum/quantum_keycodes.h"',
    ]

    # Keycodes defined by modules are in community_modules.h.
    if keyboard and keymap:
        build_dir = Path(f'{INTERMEDIATE_OUTPUT_PREFIX}{keyboard.replace("/", "_")}_{keymap}') / 'src'
        if (build_dir / 'community_modules.h').exists():
            include_dirs.append(str(build_dir))
            c_lines.append('#include "community_modules.h"')

    for header in extra_headers:
        c_lines.append(f'#include "{header.replace("\\\\", "/")}"')

    c_lines.append('int main(void) {')

    for key in keycodes:
        key = key.strip()
        if re.match(r'^[A-Z][A-Z0-9_(),|&+ ]*$', key.upper()):  # Given a key name.
            key = key.upper()
            # Do `printf("{key} = %X", {key})` to print the key's numeric value.
            c_lines.append(f'  printf("{key} = 0x%04X\\n", (uint16_t)({key}));')
        elif re.match(r'^((0x)?[0-9a-fA-F]+|[0-9]+)$', key):  # Given a numeric keycode.
            code = int(key, 0)
            if not (0 <= code <= 0xFFFF):
                raise ValueError(f'Invalid keycode: {key!r}')
            code_str = f'0x{code:04X}'
            # Call `get_keycode_string()` to convert numeric code to key name.
            c_lines.append(f'  printf("%s = {code_str}\\n", get_keycode_string({code_str}));')
        else:
            raise ValueError(f'Invalid keycode: {key!r}')

    c_lines.append('  return 0;')
    c_lines.append('}')

    return ('\n'.join(c_lines) + '\n'), include_dirs


def build_command(include_dirs: list, exe_path: str) -> list:
    """Constructs the command to build the generated C program.
    """
    cc = os.environ.get('CC', 'gcc')
    # Enough to build get_keycode_string(). EXTRAKEY_ENABLE and MOUSEKEY_ENABLE
    # are defined to enable a few more keycode names in get_keycode_string().
    return ([cc, '-xc', '-'] + [f'-I{d}' for d in include_dirs] + [
        '-DKEYCODE_STRING_ENABLE',
        '-DEXTRAKEY_ENABLE',
        '-DMOUSEKEY_ENABLE',
        'quantum/bitwise.c',
        'quantum/keycode_string.c',
        '-o',
        exe_path,
    ])


def compile_and_run(c_source: str, include_dirs: list) -> bool:
    """Compiles the C source string and runs the resulting binary.
    """
    cc = os.environ.get('CC', 'gcc')
    if not shutil.which(cc):
        cli.log.error(f"Compiler '{cc}' not found. Please install gcc or set the CC environment variable.")
        return False

    # Create temp exe path
    with tempfile.NamedTemporaryFile(suffix='.exe' if os.name == 'nt' else '', delete=False) as tmp_exe:
        exe_path = tmp_exe.name

    try:
        # Build
        cmd = build_command(include_dirs, exe_path)
        compile_proc = cli.run(cmd, capture_output=True, check=False, input=c_source)

        if compile_proc.returncode != 0:
            cli.log.error('Compilation failed!')
            cli.echo(format_build(c_source, include_dirs))
            cli.echo(f'{{fg_red}}STDERR:{{style_reset_all}}\n{compile_proc.stderr}')
            return False

        # Run
        run_proc = cli.run([exe_path], capture_output=True, text=True, check=False)
        if run_proc.returncode != 0:
            cli.log.error('Execution of compiled binary failed!')
            cli.echo(f'{{fg_red}}STDERR:{{style_reset_all}}\n{run_proc.stderr}')
            return False

        cli.echo(run_proc.stdout)
        return True

    finally:
        exe_file = Path(exe_path)
        if exe_file.exists():
            try:
                exe_file.unlink()
            except OSError:
                pass


def format_build(c_source: str, include_dirs: list) -> str:
    """Formats C code and build command for display.
    """
    c_source_lines = ''.join(f'\n{{style_dim}}{{fg_white}}{i+1:2}{{style_reset_all}} {{fg_cyan}}{line}{{style_reset_all}}' for i, line in enumerate(c_source.split('\n')[:-1]))
    cmd_str = ' '.join(shlex.quote(arg) for arg in build_command(include_dirs, '<tempfile>'))
    return (f'Generated C source:{c_source_lines}\n\n'
            f'Build command:\n{{fg_cyan}}{cmd_str}{{style_reset_all}}')


@cli.argument('keycodes', nargs='+', arg_only=True, help="Keycodes or macros to evaluate (e.g., KC_A, 'LSFT(KC_1)')")
@cli.argument('-kb', '--keyboard', type=keyboard_folder, completer=keyboard_completer, help='Keyboard name')
@cli.argument('-km', '--keymap', completer=keymap_completer, help='Keymap name')
@cli.argument('-H', '--header', action='append', default=[], arg_only=True, completer=FilesCompleter('.h'), help='Extra headers to include.')
@cli.argument('-n', '--dry-run', arg_only=True, action='store_true', help="Don't build or run, just print C source and build command.")
@cli.subcommand('Look up QMK keycode numeric value by name, or conversely keycode name by numeric value.')
@automagic_keyboard
@automagic_keymap
def lookup_keycode(cli):
    """Look up QMK keycode names and numeric values.
    """
    if not Path('quantum/keycode.h').exists():
        cli.log.error('Run this command from the root of your qmk_firmware directory.')
        return False

    keyboard = cli.config.lookup_keycode.keyboard
    keymap = cli.config.lookup_keycode.keymap

    try:
        c_source, include_dirs = generate_c_source(cli.args.keycodes, keyboard, keymap, cli.args.header)
    except ValueError as e:
        cli.log.error(str(e))
        return False

    if cli.args.dry_run:
        cli.echo(format_build(c_source, include_dirs))
        return True

    return compile_and_run(c_source, include_dirs)
