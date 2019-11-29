import argparse
import hashlib
import io
import json
import logging
import pathlib
import select
import socket
import struct
import sys
from datetime import datetime
from io import IOBase
from typing import List, Set, Dict, Optional, Tuple

import docker
import docker.errors

from cincan import registry
from cincan.command_inspector import CommandInspector
from cincan.command_log import CommandLog, FileLog, CommandLogWriter, CommandLogIndex, CommandRunner, quote_args
from cincan.configuration import Configuration
from cincan.container_check import ContainerCheck
from cincan.file_tool import FileResolver, FileMatcher
from cincan.tar_tool import TarTool


class ToolStream:
    """Handle stream to or from the container"""
    def __init__(self, stream: IOBase):
        self.data_length = 0
        self.md5 = hashlib.md5()
        self.raw = bytearray()  # when collected
        self.stream = stream

    def update(self, data: bytes):
        self.data_length += len(data)
        self.md5.update(data)


class ToolImage(CommandRunner):
    """A tool wrapped to docker image"""

    def __init__(self, name: str = None, path: Optional[str] = None,
                 image: Optional[str] = None,
                 pull: bool = False,
                 tag: Optional[str] = None,
                 rm: bool = True):
        self.config = Configuration()
        self.logger = logging.getLogger(name)
        self.client = docker.from_env()
        self.loaded_image = False  # did we load the image?
        if path is not None:
            self.name = name or path
            if tag is not None:
                self.image, log = self.client.images.build(path=path, tag=tag, rm=rm)
            else:
                self.image, log = self.client.images.build(path=path, rm=rm)
            self.context = path
            self.__log_dict_values(log)
        elif image is not None:
            self.name = name or image
            self.loaded_image = True
            if pull:
                # pull first
                self.logger.info(f"pulling image...")
                self.__get_image(image, pull=True)
            else:
                # just get the image
                try:
                    self.__get_image(image, pull=False)
                except docker.errors.ImageNotFound:
                    # image not found, try to pull it
                    self.logger.info(f"pulling image...")
                    self.__get_image(image, pull=True)
            self.context = '.'  # not really correct, but will do
        else:
            raise Exception("No file nor image specified")
        self.input_tar: Optional[str] = None  # use '-' for stdin
        self.input_filters: Optional[List[FileMatcher]] = None
        self.output_tar: Optional[str] = None  # use '-' for stdout
        self.output_dirs: List[str] = []  # output directories to create and download (filled with troves of data)
        self.upload_stats: Dict[str, List] = {} # upload file stats
        self.output_filters: Optional[List[FileMatcher]] = None

        self.network_mode: Optional[str] = None  # docker run --network=<value>
        self.user: Optional[str] = None  # docker run --user=<value>
        self.cap_add: List[str] = []  # docker run --cap-add=<value>
        self.cap_drop: List[str] = []  # docker run --cap-drop=<value>

        # more test-oriented attributes...
        self.entrypoint: Optional[str] = None
        self.upload_files: List[str] = []
        self.download_files: List[str] = []
        self.buffer_output = False

    def get_tags(self) -> List[str]:
        """List image tags"""
        return self.image.tags

    def get_id(self) -> str:
        return self.image.id

    def get_creation_time(self) -> datetime:
        """Get image creation time"""
        return registry.parse_json_time(self.image.attrs['Created'])

    def __get_image(self, image: str, pull: bool = False):
        """Get Docker image, possibly pulling it first"""
        if pull:
            self.client.images.pull(image)
        self.image = self.client.images.get(image)

    def __create_container(self, upload_files: Dict[pathlib.Path, str], input_files: List[FileLog]):
        """Create a container from the image here"""

        if self.network_mode:
            self.logger.debug(f"option network={self.network_mode}")
        if self.user:
            self.logger.debug(f"option user={self.user}")
        if self.cap_add:
            self.logger.debug("option cap-add={}".format(",".join(self.cap_add)))
        if self.cap_drop:
            self.logger.debug("option cap-drop={}".format(",".join(self.cap_drop)))

        # override entry point to just keep the container running
        container = self.client.containers.create(self.image, auto_remove=True, entrypoint="sh",
                                                  stdin_open=True, tty=True, network_mode=self.network_mode,
                                                  user=self.user, cap_add=self.cap_add, cap_drop=self.cap_drop)
        container.start()

        # kludge, lets show work directory in tests
        work_dir = container.image.attrs['Config'].get('WorkingDir') or '/'
        if self.entrypoint:
            self.logger.info(f"Workdir: {work_dir}")

        # upload files to container
        tar_tool = TarTool(self.logger, container, self.upload_stats, explicit_file=self.input_tar)
        tar_tool.upload(upload_files, input_files)

        return container

    def __unpack_container_stream(self, c_socket) -> Tuple[int, bytes]:
        """Unpack bytes coming from container stream"""
        buf = bytearray()
        while len(buf) < 8:
            r = c_socket.read(8 - len(buf))
            if not r:
                return 0, bytes([])  # EOF
            buf.extend(r)
        s_len = struct.unpack('>Q', buf)[0]
        s_type = s_len >> 56
        s_len = s_len & 0xffffffffffffff

        self.logger.debug(f"container input type={s_type} length={s_len}")
        buf.clear()
        while len(buf) < s_len:
            r = c_socket.read(s_len - len(buf))
            if not r:
                raise Exception('Failed to read all data from the container')
            buf.extend(r)
        return s_type, buf

    def __container_exec(self, container, cmd_args: List[str], read_stdin: bool, write_stdout: bool) -> CommandLog:
        """Execute a command in the container"""
        # create the full command line and run with exec
        entry_point = [self.entrypoint] if self.entrypoint else self.image.attrs['Config'].get('Entrypoint')
        if not entry_point:
            entry_point = []
        cmd = self.image.attrs['Config'].get('Cmd')
        if not cmd:
            cmd = []  # 'None' value observed
        user_cmd = (cmd_args if cmd_args else cmd)
        full_cmd = entry_point + user_cmd

        log = CommandLog([self.name] + user_cmd)

        stdin_s = ToolStream(sys.stdin) if read_stdin else None
        stdout_s = ToolStream(sys.stdout.buffer) if write_stdout else None
        stderr_s = ToolStream(sys.stderr.buffer)

        is_tty = sys.stdout.isatty()  # follow stdout, as stdout and stderr are returned the same with TTY
        self.logger.debug(f"exec tty={is_tty}")

        # execute the command, collect stdin and stderr
        exec = self.client.api.exec_create(container.id, cmd=full_cmd, stdin=read_stdin, tty=is_tty)
        exec_id = exec['Id']
        c_socket = self.client.api.exec_start(exec_id, detach=False, socket=True)
        c_socket_sock = c_socket._sock  # NOTE: c_socket itself is not writeable???, but this is :O

        buffer_size = 1024 * 1024

        self.logger.debug("enter stdin/container io loop...")
        active_streams = [c_socket_sock]  # prefer socket to limit the amount of data in the container (?)
        if read_stdin:
            active_streams.append(sys.stdin)
        c_socket_open = True
        while c_socket_open:
            try:
                # FIXME: Using select, which is known not to work with Windows!
                select_in, _, _ = select.select(active_streams, [], [])
            except io.UnsupportedOperation as e:
                if sys.stdin in active_streams:
                    # pytest stdin is somehow fundamentally dysfunctional
                    active_streams.remove(sys.stdin)
                    continue
                raise e

            for sel in select_in:
                if sel == sys.stdin:
                    s_data = sys.stdin.buffer.read(buffer_size)
                    if not s_data:
                        self.logger.debug(f"received eof from stdin")
                        active_streams.remove(sel)
                        c_socket_sock.shutdown(socket.SHUT_WR)
                    else:
                        self.logger.debug(f"received {len(s_data)} bytes from stdin")
                        stdin_s.update(s_data)

                        out_off = 0
                        while out_off < len(s_data):
                            out_off += c_socket_sock.send(s_data[out_off:])
                            self.logger.debug(f"wrote ...{out_off} bytes to container stdin")

                elif sel == c_socket_sock:
                    s_type, s_data = self.__unpack_container_stream(c_socket)
                    if not s_data:
                        self.logger.debug(f"received eof from container")
                        c_socket_open = False
                        continue
                    if s_type == 1:
                        self.logger.debug(f"received {len(s_data)} bytes from stdout")
                        std_s = stdout_s
                    elif s_type == 2:
                        self.logger.debug(f"received {len(s_data)} bytes from stderr")
                        std_s = stderr_s
                    else:
                        self.logger.warning(f"received {len(s_data)} bytes from ???, discarding")
                        std_s = None
                    if std_s:
                        std_s.update(s_data)
                        if self.buffer_output:
                            std_s.raw.extend(s_data)
                        else:
                            std_s.stream.write(s_data)

        # inspect execution result
        inspect = self.client.api.exec_inspect(exec_id)
        log.exit_code = inspect.get('ExitCode', 0)

        # collect raw data
        if self.buffer_output:
            log.stdin = bytes(stdin_s.raw) if stdin_s else b''
            log.stdout = bytes(stdout_s.raw) if stdout_s else b''
            log.stderr = bytes(stderr_s.raw) if stderr_s else b''

        if log.exit_code == 0:
            # collect stdin, stdout, stderr hash codes
            if stdin_s and stdin_s.data_length:
                log.in_files.append(FileLog(pathlib.Path('/dev/stdin'), stdin_s.md5.hexdigest()))
            if stdout_s and stdout_s.data_length:
                log.out_files.append(FileLog(pathlib.Path('/dev/stdout'), stdout_s.md5.hexdigest()))
            if stderr_s and stderr_s.data_length:
                log.out_files.append(FileLog(pathlib.Path('/dev/stderr'), stderr_s.md5.hexdigest()))

        return log

    def __run(self, args: List[str]) -> CommandLog:
        """Run native tool in container with given arguments"""
        # resolve files to upload
        resolver = FileResolver(args, pathlib.Path.cwd(), do_resolve=not self.input_tar,
                                output_dirs=self.output_dirs, input_filters=self.input_filters)
        upload_files = {}
        cmd_args = resolver.resolve_upload_files(upload_files)
        for h_file, a_name in upload_files.items():
            self.logger.debug(f"{h_file.as_posix()} -> {a_name}")
        self.logger.debug("args: %s", ' '.join(quote_args(cmd_args)))

        in_files = []
        container = self.__create_container(upload_files, in_files)
        log = CommandLog([])
        try:
            log = self.__container_exec(container, cmd_args,
                                        read_stdin=(self.input_tar != '-'), write_stdout=(self.output_tar != '-'))
            log.in_files.extend(in_files)
            if log.exit_code == 0:
                # download results
                tar_tool = TarTool(self.logger, container, self.upload_stats, explicit_file=self.output_tar)
                log.out_files.extend(tar_tool.download_files(self.output_filters))
        finally:
            self.logger.debug("killing the container")
            container.kill()
            # if we created the image, lets also remove it (intended for testing)
            if not self.loaded_image:
                self.logger.info(f"removing the docker image {self.get_id()}")
                try:
                    self.remove_image()
                except docker.errors.APIError as e:
                    self.logger.warning(e)

        work_dir = pathlib.Path().cwd()
        self.upload_files = sorted([f.as_posix() for f in list(upload_files.keys())])
        self.download_files = sorted(
            [f.path.relative_to(work_dir).as_posix() for f in
             filter(lambda f: not f.path.as_posix().startswith('/dev/'), log.out_files)])
        return log

    def run(self, args: List[str]) -> CommandLog:
        """Run native tool in container, return output"""
        self.buffer_output = False  # we stream it
        return self.__run(args)

    def run_get_string(self, args: List[str]) -> str:
        """Run native tool in container, return output as a string"""
        self.buffer_output = True  # we return it
        log = self.__run(args)
        return log.stdout.decode('utf8') + log.stderr.decode('utf8')

    def __log_dict_values(self, log: Set[Dict[str, str]]) -> None:
        """Log values from a dict as debug"""
        for i in log:
            v = i.values()
            self.logger.debug("{}".format(*v).strip())

    def file_to_copy_from_context(self, file: str) -> str:
        """Create path for sample file inside docker context (for unit testing) """
        return str(pathlib.Path(self.context) / file)

    def remove_image(self):
        """Remove this image"""
        self.client.images.remove(self.get_id())


def image_default_args(sub_parser):
    """Default arguments for sub commands which load docker image"""
    sub_parser.add_argument('tool', help="the tool and possible arguments", nargs=argparse.REMAINDER)
    sub_parser.add_argument('-p', '--path', help='path to Docker context')
    sub_parser.add_argument('-u', '--pull', action='store_true', help='Pull image from registry')
    sub_parser.add_argument('--network', nargs='?', help='Container network (same as docker run --network)')
    sub_parser.add_argument('--user', nargs='?', help='User in container (same as docker run --user)')
    sub_parser.add_argument('--cap-add', action='append', dest='cap_add', nargs='?',
                            help='Add Linux capability, use many times if required')
    sub_parser.add_argument('--cap-drop', action='append', dest='cap_drop', nargs='?',
                            help='Drop Linux capability, use many times if required')

    sub_parser.add_argument('-d', '--mkdir', action='append', dest='output_dir', nargs='?',
                            help='Force an empty directory to container')

    sub_parser.add_argument('-i', '--in', dest='input_tar', nargs='?',
                            help='Provide the input files to load unfiltered into the container working directory')
    sub_parser.add_argument('-o', '--out', dest='output_tar', nargs='?',
                            help='Upload output files into specified tar archive')

    sub_parser.add_argument('-I', '--in-filter', action='append', dest='in_filter', nargs='?',
                            help='Filter input files by pattern (* as wildcard, ^-prefix for inverse filter)')
    sub_parser.add_argument('-O', '--out-filter', action='append', dest='out_filter', nargs='?',
                            help='Include output files by pattern (* as wildcard, ^-prefix for inverse filter)')


def main():
    """Parse command line and run the tool"""
    m_parser = argparse.ArgumentParser()
    m_parser.add_argument("-l", "--log", dest="log_level", choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
                          help="Set the logging level", default=None)
    m_parser.add_argument('-q', '--quiet', action='store_true', help='Be quite quiet')
    subparsers = m_parser.add_subparsers(dest='sub_command')

    run_parser = subparsers.add_parser('run')
    image_default_args(run_parser)

    test_parser = subparsers.add_parser('test')
    image_default_args(test_parser)

    list_parser = subparsers.add_parser('list')
    list_parser.add_argument('-t', '--tags', action='store_true', help='Show tags')

    mani_parser = subparsers.add_parser('manifest')
    image_default_args(mani_parser)

    fanin_parser = subparsers.add_parser('fanin', help='Show fan-in to the given file')
    fanin_parser.add_argument('file', help="File to analyze")
    fanout_parser = subparsers.add_parser('fanout', help='Show fan-out from the given file')
    fanout_parser.add_argument('file', help="File to analyze")

    help_parser = subparsers.add_parser('help')

    if len(sys.argv) > 1:
        args = m_parser.parse_args(args=sys.argv[1:])
    else:
        args = m_parser.parse_args(args=['help'])

    log_level = args.log_level if args.log_level else ('WARNING' if args.quiet else 'INFO')
    if log_level not in {'DEBUG'}:
        sys.tracebacklimit = 0  # avoid track traces unless debugging
    logging.basicConfig(format='%(name)s: %(message)s', level=getattr(logging, log_level))

    sub_command = args.sub_command
    if sub_command == 'help':
        m_parser.print_help()
        sys.exit(1)
    elif sub_command in {'run', 'test'}:
        if len(args.tool) == 0:
            raise Exception('Missing tool name argument')
        name = args.tool[0]
        if args.path is None:
            tool = ToolImage(name, image=name, pull=args.pull)
        elif args.path is not None:
            tool = ToolImage(name, path=args.path)
        else:
            tool = ToolImage(name)  # should raise exception
        tool.input_tar = args.input_tar if args.input_tar else None
        tool.output_tar = args.output_tar if args.output_tar else None
        tool.output_dirs = args.output_dir or []
        tool.input_filters = FileMatcher.parse(args.in_filter) if args.in_filter is not None else None
        tool.output_filters = FileMatcher.parse(args.out_filter) if args.out_filter is not None else None

        if tool.input_tar and tool.input_filters:
            raise Exception("Cannot specify input filters with input tar file")

        tool.network_mode = args.network
        tool.user = args.user
        tool.cap_add = args.cap_add
        tool.cap_drop = args.cap_drop

        all_args = args.tool[1:]
        if sub_command == 'test':
            check = ContainerCheck(tool)
            tool.logger.info("# {} {}".format(','.join(tool.get_tags()), registry.format_time(tool.get_creation_time())))
            log = check.run(all_args)
        else:
            log = tool.run(all_args)

        if log.exit_code == 0:
            if tool.config.is_command_log():
                log_writer = CommandLogWriter()
                log_writer.write(log)
        if log.stdout:
            sys.stdout.buffer.write(log.stdout)
        if log.stderr:
            sys.stderr.buffer.write(log.stderr)
        sys.exit(log.exit_code)  # exit code
    elif sub_command in {'fanin', 'fanout'}:
        inspector = CommandInspector(CommandLogIndex(), pathlib.Path().resolve())
        if sub_command == 'fanout':
            res = inspector.fanout(pathlib.Path(args.file).resolve())
        else:
            res = inspector.fanin(pathlib.Path(args.file).resolve())
        print(res)
    elif sub_command == 'manifest':
        # sub command 'manifest'
        if len(args.tool) == 0:
            raise Exception('Missing tool name argument')
        name = args.tool[0]
        reg = registry.ToolRegistry()
        info = reg.fetch_manifest(name)
        print(json.dumps(info, indent=2))
    elif sub_command == 'list':
        format_str = "{0:<25}"
        if args.tags:
            format_str += " {4:<20}"
        format_str += " {1}"
        reg = registry.ToolRegistry()
        tool_list = reg.list_tools()
        for tool in sorted(tool_list):
            lst = tool_list[tool]
            print(format_str.format(lst.name, lst.description, ",".join(lst.input), ",".join(lst.output),
                                    ",".join(lst.tags)))
    else:
        raise Exception(f"Unexpected sub command '{sub_command}")
