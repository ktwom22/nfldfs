from flask import Flask, render_template, request, redirect, url_for, session
import pandas as pd
from pydfs_lineup_optimizer import get_optimizer, Site, Sport, Player
from pydfs_lineup_optimizer.stacks import PositionsStack
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)

app = Flask(__name__)
app.secret_key = "your-secret-key"

CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQEcvQUS_HIbxKp4SbD5HUMJvhLr7tP6yXNVHMul6Ad2PrIQZF9VKgqAmESJBp4CkjfcDxvClpBqK6M/pub?gid=1236050410&single=true&output=csv"

DISPLAY_COLUMNS = [
    "NAME", "SALARY", "TEAM", "OPP", "DVP", "VALUE", "L5 AVG", "L10 AVG", "SZ AVG",
    "O/U", "TM PTS", "OWN %", "ADJ PROJECTION", "POS", "GAME TIME"
]

COLUMN_MAP = {
    'PROJECTED POINTS': 'FINAL PROJECTION',
    'PLAYER': 'NAME',
    'GAME TIME': 'GAME TIME',
    'TEAM': 'TEAM',
    'OPP': 'OPP',
    'DVP': 'DVP',
    'VALUE': 'VALUE',
    'L5 AVG': 'L5 AVG',
    'L10 AVG': 'L10 AVG',
    'SZ AVG': 'SZ AVG',
    'O/U': 'O/U',
    'TM PTS': 'TM PTS',
    'OWN %': 'OWN %',
    'SALARY': 'SALARY',
    'POSITION': 'POS'
}


# ---------------- Utility Functions ----------------
def display_pos(positions):
    if isinstance(positions, str):
        pos_list = [p.strip().upper() for p in positions.replace("/", ",").split(",") if p.strip()]
    else:
        pos_list = [p.upper() for p in positions]
    return "/".join(sorted(set(pos_list)))


def safe_float(val, default=0.0):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def compute_adjusted_proj(row):
    proj = safe_float(row.get("FINAL PROJECTION", 0))
    dvp = safe_float(row.get("DVP", 0))
    l5 = safe_float(row.get("L5 AVG", 0))
    szn = safe_float(row.get("SZ AVG", 0))
    adj = proj
    if dvp < 5:
        adj -= 1.5
    if abs(l5 - szn) <= 5 and l5 > 14:
        adj += 1.5
    adj *= 0.75
    return round(adj, 2)


# ---------------- Game Time Filter ----------------
def filter_by_game_time(df, time_filter):
    df = df.copy()

    def parse_hour_minute(game_time_str):
        if not isinstance(game_time_str, str) or ":" not in game_time_str:
            return None
        try:
            parts = game_time_str.strip().split(":")
            hour = int(parts[0])
            minute = int(parts[1][:2])
            am_pm = game_time_str.strip()[-2:].upper()
            if am_pm == "PM" and hour != 12:
                hour += 12
            elif am_pm == "AM" and hour == 12:
                hour = 0
            return hour * 60 + minute
        except:
            return None

    df["GAME_MIN"] = df["GAME TIME"].apply(parse_hour_minute)

    if time_filter == "all":
        filtered = df.dropna(subset=["GAME_MIN"])
    elif time_filter == "1pm":
        filtered = df[(df["GAME_MIN"] >= 12 * 60 + 30) & (df["GAME_MIN"] <= 13 * 60 + 30)]
    elif time_filter == "late":
        filtered = df[df["GAME_MIN"] >= 16 * 60]
    else:
        filtered = df.dropna(subset=["GAME_MIN"])

    return filtered


# ---------------- Load Players ----------------
def load_players():
    df_raw = pd.read_csv(CSV_URL)
    df_raw.columns = [c.strip().upper() for c in df_raw.columns]
    df_raw = df_raw.rename(columns={k.upper(): v for k, v in COLUMN_MAP.items()})
    df_raw = df_raw.loc[:, ~df_raw.columns.duplicated(keep="first")]

    for col in DISPLAY_COLUMNS:
        if col not in df_raw.columns:
            df_raw[col] = ""

    # Parse salary
    if "SALARY" in df_raw.columns:
        def parse_salary(x):
            try:
                val = str(x).replace("$", "").replace(",", "").replace("k", "")
                f = float(val)
                return int(f * 1000) if f < 100 else int(f)
            except:
                return 0

        df_raw["SALARY"] = df_raw["SALARY"].apply(parse_salary)

    df_raw = df_raw[df_raw["SALARY"] > 0]
    df_raw["FINAL PROJECTION"] = pd.to_numeric(df_raw.get("FINAL PROJECTION", 0), errors="coerce").fillna(0)
    df_raw["ADJ PROJECTION"] = df_raw.apply(compute_adjusted_proj, axis=1)
    df_raw = df_raw[df_raw["FINAL PROJECTION"] > 0]

    df_raw["POS"] = df_raw["POS"].astype(str).str.strip().str.upper()
    df_raw["unique_id"] = df_raw.apply(lambda row: f"{row.get('NAME', '')}_{row.name}", axis=1)
    all_teams = sorted([team for team in df_raw["TEAM"].dropna().astype(str).str.upper().unique() if team.strip()])

    return df_raw, all_teams


# ---------------- Build Lineups ----------------
def build_lineups(df, num_lineups=1, locked_ids=None, excluded_ids=None, stack_team=None):
    optimizer = get_optimizer(Site.DRAFTKINGS, Sport.FOOTBALL)
    players = []

    DK_NFL_SLOTS = ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "DST"]

    if excluded_ids:
        df = df[~df["unique_id"].isin(excluded_ids)]

    for _, row in df.iterrows():
        positions = [p.strip().upper() for p in str(row.get("POS", "")).split("/")]
        salary = int(row.get("SALARY", 0) or 0)
        fppg = safe_float(row.get("ADJ PROJECTION", 0))
        if salary <= 0 or fppg <= 0 or not positions or positions == [""]:
            continue
        player = Player(
            player_id=row["unique_id"],
            first_name=row.get("NAME", ""),
            last_name="",
            positions=positions,
            team=row.get("TEAM", ""),
            salary=salary,
            fppg=fppg
        )
        players.append(player)

    optimizer.load_players(players)

    # Stack selected team if provided
    if stack_team:
        try:
            optimizer.add_stack(
                PositionsStack(
                    ["QB", ("RB", "WR", "TE"), ("RB", "WR", "TE")],
                    for_positions=["QB", "RB", "WR", "TE"],
                    team=stack_team
                )
            )
        except Exception as e:
            print("Stacking error:", e)

    optimizer.set_total_teams(min_teams=4, max_teams=6)

    if locked_ids:
        for pid in locked_ids:
            p = next((pl for pl in players if pl.id == pid), None)
            if p:
                optimizer.add_player_to_lineup(p)

    # Generate lineups
    unique_lineups, seen_lineups = [], set()
    try:
        for lineup in optimizer.optimize(n=num_lineups * 3):
            ids = tuple(sorted([p.id for p in lineup.players]))
            if ids in seen_lineups:
                continue
            seen_lineups.add(ids)
            slot_players = [(slot, p) for slot, p in zip(DK_NFL_SLOTS, lineup.players)]
            unique_lineups.append(slot_players)
            if len(unique_lineups) >= num_lineups:
                break
    except Exception as e:
        print("Optimizer error:", e)

    return unique_lineups


# ---------------- Flask Routes ----------------
@app.route("/", methods=["GET", "POST"])
def player_pool():
    df, teams = load_players()
    locked_players = session.get("locked_players", [])
    excluded_players = session.get("excluded_players", [])
    num_lineups = session.get("num_lineups", 1)
    time_filter = session.get("time_filter", "all")
    stack_team = session.get("stack_team", "")

    if request.method == "POST":
        locked_players = request.form.getlist("lock_player")
        excluded_players = request.form.getlist("exclude_player")
        num_lineups = int(request.form.get("num_lineups", 1))
        time_filter = request.form.get("time_filter", "all")
        stack_team = request.form.get("stack_team", "")

        session["locked_players"] = locked_players
        session["excluded_players"] = excluded_players
        session["num_lineups"] = num_lineups
        session["time_filter"] = time_filter
        session["stack_team"] = stack_team

        return redirect(url_for("lineups_page"))

    filtered_df = filter_by_game_time(df, time_filter)
    if excluded_players:
        filtered_df = filtered_df[~filtered_df["unique_id"].isin(excluded_players)]

    data = filtered_df.fillna("").to_dict(orient="records")

    return render_template(
        "player_pool.html",
        headers=DISPLAY_COLUMNS,
        data=data,
        teams=teams,
        locked_players=locked_players,
        excluded_players=excluded_players,
        display_pos=display_pos,
        num_lineups=num_lineups,
        time_filter=time_filter,
        stack_team=stack_team
    )


@app.route("/lineups", methods=["GET"])
def lineups_page():
    df, teams = load_players()
    locked_players = session.get("locked_players", [])
    excluded_players = session.get("excluded_players", [])
    num_lineups = int(session.get("num_lineups", 1))
    time_filter = session.get("time_filter", "all")
    stack_team = session.get("stack_team", "")

    filtered_df = filter_by_game_time(df, time_filter)
    if excluded_players:
        filtered_df = filtered_df[~filtered_df["unique_id"].isin(excluded_players)]

    lineups = build_lineups(filtered_df, num_lineups, locked_players, excluded_players, stack_team)

    return render_template(
        "lineups.html",
        lineups=lineups,
        num_lineups=num_lineups,
        display_pos=display_pos,
        time_filter=time_filter
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5005, debug=True, use_reloader=False)
