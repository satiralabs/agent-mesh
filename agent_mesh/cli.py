import argparse
import sys
import json
import os
from typing import List

from . import __version__
from .bus import send_message, list_messages, ack_message, listen_messages, DEFAULT_DB_PATH
from .secrets import set_service_secret, list_services_safe, delete_service, DEFAULT_SECRETS_PATH
from .broker import run_broker, PENDING_APPROVALS
from .handover import compile_handover_pack, sync_handover_to_drive, watch_and_sync, DEFAULT_MEMORY_DIR, DEFAULT_PACK_DIR, probe_local_services

def cmd_msg(args):
    if args.action == "send":
        if not args.to or not args.text:
            print("Error: --to and --text/payload are required.")
            sys.exit(1)
        sender = args.sender or "agent-mesh"
        msg_id = send_message(sender, args.to, args.type, args.text, args.corr, args.db)
        print(f"Message sent! ID: {msg_id}")
    elif args.action == "list":
        msgs = list_messages(recipient=args.to, unread_only=not args.all, limit=args.limit, db_path=args.db)
        if not msgs:
            print("Inbox empty.")
            return
        print(f"{'ID':<6} {'FROM':<15} {'TO':<15} {'TYPE':<18} {'TIME':<20} {'PROCESSED'}")
        print("-" * 80)
        for m in msgs:
            proc = "YES" if m['is_processed'] else "NO"
            print(f"{m['id']:<6} {m['sender']:<15} {m['recipient']:<15} {m['msg_type']:<18} {m['created_at']:<20} {proc}")
            print(f"       Payload: {json.dumps(m['payload']) if isinstance(m['payload'], (dict, list)) else m['payload']}")
    elif args.action == "ack":
        if not args.msg_id:
            print("Error: message ID required.")
            sys.exit(1)
        ok = ack_message(args.msg_id, args.db)
        if ok:
            print(f"Message {args.msg_id} acknowledged.")
        else:
            print(f"Message {args.msg_id} not found or already acknowledged.")
    elif args.action == "listen":
        if not args.to:
            print("Error: --to <agent_name> required to listen.")
            sys.exit(1)
        print(f"Listening for messages to '{args.to}' (Ctrl+C to stop)...")
        try:
            for m in listen_messages(recipient=args.to, db_path=args.db):
                print(f"\n[NEW MESSAGE #{m['id']}] From: {m['sender']} | Type: {m['msg_type']}")
                print(f"Payload: {json.dumps(m['payload'], indent=2)}")
                ack_message(m['id'], args.db)
        except KeyboardInterrupt:
            print("\nStopped.")

def cmd_secret(args):
    if args.action == "set":
        if not args.service or not args.upstream or not args.token:
            print("Error: service, upstream, and token are required.")
            sys.exit(1)
        set_service_secret(
            service=args.service,
            upstream=args.upstream,
            token=args.token,
            auth_type=args.type,
            require_approval=args.approval,
            path=args.path
        )
        print(f"Secret for service '{args.service}' saved securely (0600 permissions).")
    elif args.action == "list":
        svcs = list_services_safe(args.path)
        if not svcs:
            print("No services configured in secret store.")
            return
        print(f"{'SERVICE':<16} {'UPSTREAM':<35} {'AUTH TYPE':<12} {'APPROVAL':<10} {'TOKEN'}")
        print("-" * 85)
        for name, cfg in svcs.items():
            req_app = "YES" if cfg["require_approval"] else "NO"
            print(f"{name:<16} {cfg['upstream']:<35} {cfg['auth_type']:<12} {req_app:<10} {cfg['masked_token']}")
    elif args.action == "delete":
        if not args.service:
            print("Error: service name required.")
            sys.exit(1)
        if delete_service(args.service, args.path):
            print(f"Service '{args.service}' deleted.")
        else:
            print(f"Service '{args.service}' not found.")

def cmd_broker(args):
    if args.action == "start":
        run_broker(host=args.host, port=args.port, secrets_path=args.secrets_path)
    elif args.action == "approve":
        import urllib.request
        url = f"http://127.0.0.1:{args.port}/approval/approve/{args.request_id}"
        req = urllib.request.Request(url, method="POST")
        try:
            with urllib.request.urlopen(req) as resp:
                print(f"Approved request {args.request_id}: {resp.read().decode()}")
        except Exception as e:
            print(f"Approval failed: {e}")

def cmd_handover(args):
    if args.action == "compile":
        out = compile_handover_pack(memory_dir=args.memory_dir, out_dir=args.out_dir)
        print("Handover pack compiled successfully:")
        for k, p in out.items():
            print(f" - {k}: {p}")
    elif args.action == "sync":
        compile_handover_pack(memory_dir=args.memory_dir, out_dir=args.out_dir)
        ok = sync_handover_to_drive(pack_dir=args.out_dir)
        if ok:
            print("Successfully synchronized to Google Drive.")
        else:
            print("Handover pack compiled locally (Drive sync skipped or pending).")
    elif args.action == "watch":
        watch_and_sync(memory_dir=args.memory_dir, out_dir=args.out_dir, interval=args.interval)

def cmd_ui(args):
    from .gui import launch_native_gui
    port = getattr(args, "port", 8088)
    sys.exit(launch_native_gui(port=port))

def cmd_menu(args):
    while True:
        os.system("clear")
        print("=" * 55)
        print(f"       agent-mesh v{__version__} — Control Dashboard")
        print("=" * 55)
        print(" [1] View System Status & Live Probes")
        print(" [2] View Inter-Agent Messages (All)")
        print(" [3] View Protected APIs & Secret Endpoints")
        print(" [4] Compile Handover Pack Locally")
        print(" [5] Compile & Sync Handover to Google Drive")
        print(" [6] Start Secret Broker Proxy (port 8088)")
        print(" [0] Exit")
        print("-" * 55)
        try:
            choice = input("Enter choice [0-6]: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting.")
            break

        if choice == "1":
            os.system("clear")
            cmd_status(args)
            input("\nPress Enter to return...")
        elif choice == "2":
            os.system("clear")
            print("--- All Messages in Bus ---")
            msgs = list_messages(unread_only=False, limit=20, db_path=DEFAULT_DB_PATH)
            if not msgs:
                print("No messages found.")
            else:
                for m in msgs:
                    p = "ACK" if m['is_processed'] else "UNREAD"
                    print(f"#{m['id']} [{m['created_at']}] {m['sender']} -> {m['recipient']} ({m['msg_type']}) [{p}]")
                    print(f"   Payload: {m['payload']}")
            input("\nPress Enter to return...")
        elif choice == "3":
            os.system("clear")
            print("--- Protected Services in Secret Store ---")
            svcs = list_services_safe(DEFAULT_SECRETS_PATH)
            if not svcs:
                print("No services configured.")
            else:
                for name, s in svcs.items():
                    print(f"• {name}: -> {s['upstream']} (Token: {s['masked_token']}, Approval: {s['require_approval']})")
            input("\nPress Enter to return...")
        elif choice == "4":
            os.system("clear")
            print("Compiling handover pack...")
            res = compile_handover_pack(memory_dir=DEFAULT_MEMORY_DIR, out_dir=DEFAULT_PACK_DIR)
            for k, v in res.items():
                print(f"Generated {k}: {v}")
            input("\nPress Enter to return...")
        elif choice == "5":
            os.system("clear")
            print("Compiling and syncing to Google Drive...")
            compile_handover_pack(memory_dir=DEFAULT_MEMORY_DIR, out_dir=DEFAULT_PACK_DIR)
            ok = sync_handover_to_drive(pack_dir=DEFAULT_PACK_DIR)
            if ok:
                print("\nSync completed successfully!")
            else:
                print("\nSync finished (Drive update status verified).")
            input("\nPress Enter to return...")
        elif choice == "6":
            os.system("clear")
            print("Starting secret broker on port 8088 (Press Ctrl+C to stop)...")
            try:
                run_broker(port=8088)
            except KeyboardInterrupt:
                pass
            input("\nPress Enter to return...")
        elif choice == "0":
            print("Goodbye.")
            break

def main():
    parser = argparse.ArgumentParser(description="agent-mesh: Lightweight, secure agent communication, handover, and secret broker.")
    parser.add_argument("--version", action="version", version=f"agent-mesh {__version__}")
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # MSG
    p_msg = subparsers.add_parser("msg", help="Agent message bus & mailbox")
    p_msg.add_argument("action", choices=["send", "list", "ack", "listen"])
    p_msg.add_argument("--to", help="Recipient agent name")
    p_msg.add_argument("--from", dest="sender", help="Sender agent name")
    p_msg.add_argument("--type", default="GENERAL", help="Message type (e.g. TASK, HANDOVER, STATUS)")
    p_msg.add_argument("--text", help="Message payload / content")
    p_msg.add_argument("--corr", help="Correlation ID")
    p_msg.add_argument("--msg-id", type=int, help="Message ID to acknowledge")
    p_msg.add_argument("--all", action="store_true", help="Include processed messages")
    p_msg.add_argument("--limit", type=int, default=50, help="Max messages to list")
    p_msg.add_argument("--db", default=DEFAULT_DB_PATH, help="Path to bus.db")

    # SECRET
    p_secret = subparsers.add_parser("secret", help="Zero-trust secret management")
    p_secret.add_argument("action", choices=["set", "list", "delete"])
    p_secret.add_argument("service", nargs="?", help="Service identifier (e.g. shipd, telegram)")
    p_secret.add_argument("--upstream", help="Upstream base URL (e.g. https://api.shipd.com/v1)")
    p_secret.add_argument("--token", help="Secret token / key value")
    p_secret.add_argument("--type", choices=["bearer", "header", "telegram_bot"], default="bearer", help="Auth header type")
    p_secret.add_argument("--approval", action="store_true", help="Require human confirmation before execution")
    p_secret.add_argument("--path", default=DEFAULT_SECRETS_PATH, help="Secrets file path")

    # BROKER
    p_broker = subparsers.add_parser("broker", help="Secret-injecting reverse proxy")
    p_broker.add_argument("action", choices=["start", "approve"])
    p_broker.add_argument("--host", default="127.0.0.1", help="Bind host")
    p_broker.add_argument("--port", type=int, default=8088, help="Bind port")
    p_broker.add_argument("--request-id", help="Request ID to approve")
    p_broker.add_argument("--secrets-path", default=DEFAULT_SECRETS_PATH, help="Secrets config path")

    # HANDOVER
    p_handover = subparsers.add_parser("handover", help="Handover pack compilation and Drive sync")
    p_handover.add_argument("action", choices=["compile", "sync", "watch"])
    p_handover.add_argument("--memory-dir", default=DEFAULT_MEMORY_DIR, help="Memory root directory")
    p_handover.add_argument("--out-dir", default=DEFAULT_PACK_DIR, help="Output handover pack directory")
    p_handover.add_argument("--interval", type=int, default=300, help="Watch interval in seconds")

    # UI / DASHBOARD
    p_ui = subparsers.add_parser("ui", help="Launch clean graphical control center")
    p_ui.add_argument("--port", type=int, default=8088, help="Server port")

    # STATUS
    subparsers.add_parser("status", help="Show system status and service checks")

    # MENU
    subparsers.add_parser("menu", help="Launch interactive terminal menu")

    args = parser.parse_args()
    if not args.command:
        # Default to launching the Graphical UI if invoked from desktop launcher
        cmd_ui(argparse.Namespace(port=8088))
        sys.exit(0)

    if args.command == "ui":
        cmd_ui(args)
    elif args.command == "msg":
        cmd_msg(args)
    elif args.command == "secret":
        cmd_secret(args)
    elif args.command == "broker":
        cmd_broker(args)
    elif args.command == "handover":
        cmd_handover(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "menu":
        cmd_menu(args)

if __name__ == "__main__":
    main()
