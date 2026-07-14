# -*- coding: utf-8 -*-
"""DFM 角色注册表：把社区模型文件名 →（中文名 / 分类 / 合规等级），并合并体检指标，
产出统一角色目录，供画廊 UI 展示、换脸 lab 加载、Hub 按角色下发共用一个真相源。

为什么需要：社区文件名是英文 snake_case（jason_statham_320），用户看不懂；且这些都是
真人名人脸，涉及肖像权/深伪合规——必须按分类分级（政治人物默认下架，名人标"演示级"），
不能一股脑全曝光给终端用户。此表是"能不能给用户看、给谁看"的策略闸口。
"""
import re, json
from pathlib import Path

BASE = Path(r"C:\模仿音色")
COMMUNITY = BASE / "_pending_models" / "community"
REPORT = BASE / "dfm_workspace" / "_community_report.json"
THUMBS = BASE / "dfm_workspace" / "_community_thumbs"
REGISTRY_OUT = BASE / "dfm_workspace" / "dfm_registry.json"

# 合规等级：
#   ok     = 名人/演示级，可进画廊（默认展示，输出必带水印+溯源）
#   caution= 网红/素人，展示但标注需本人授权
#   blocked= 政治人物/公众政要/深伪诈骗高发人物，默认下架（误导风险最高）——不进用户画廊
# S7: 集合更名语义扩展——不只政客，还含「深伪诈骗高发脸」（MrBeast 本人多次公开控诉
#   自己的脸被深伪拿去做带货诈骗广告；直播带货场景用他 = 正撞枪口，循证升格 blocked）。
POLITICIANS = {
    "donald trump", "trump", "joe biden", "biden", "barack obama", "obama", "kamala harris",
    "hillary clinton", "bill clinton", "chelsea clinton", "hunter biden", "ben shapiro",
    "jordan peterson", "elizabeth warren", "pete buttigieg", "ron desantis", "aoc",
    "alexandria ocasio cortez", "george bush", "george w bush", "jfk", "john kennedy",
    "abe lincoln", "abraham lincoln", "tucker carlson", "chris cuomo", "christopher cuomo",
    "andrew tate", "elon musk", "mark zuckerberg", "kamala",
    "jimmy donaldson", "mrbeast",
    "ron de santis",     # S7: norm_key 现做驼峰拆词，"DeSantis"→"de santis"，补该形态防漏判
}

# 英文 token → (中文名, 分类)。分类：hollywood/asian/music/influencer/character/other
NAMES = {
    "adam levine": ("亚当·莱文", "music"),
    "adrianne palicki": ("阿德里安娜·帕丽奇", "hollywood"),
    "agnetha falskog": ("阿格妮莎·费尔茨科格", "music"),
    "alan ritchson": ("艾伦·里奇森", "hollywood"),
    "alicia vikander": ("艾丽西亚·维坎德", "hollywood"),
    "amber midthunder": ("安柏·米德桑德", "hollywood"),
    "andras arato": ("安德拉斯·阿拉托", "influencer"),
    "angelina jolie": ("安吉丽娜·朱莉", "hollywood"),
    "anne hathaway": ("安妮·海瑟薇", "hollywood"),
    "anya chalotra": ("安雅·查洛特拉", "hollywood"),
    "arnold schwarzenegger": ("阿诺·施瓦辛格", "hollywood"),
    "benjamin affleck": ("本·阿弗莱克", "hollywood"),
    "benjamin stiller": ("本·斯蒂勒", "hollywood"),
    "bradley pitt": ("布拉德·皮特", "hollywood"),
    "brad pitt": ("布拉德·皮特", "hollywood"),
    "brie larson": ("布丽·拉尔森", "hollywood"),
    "bruce campbell": ("布鲁斯·坎贝尔", "hollywood"),
    "bruce willis": ("布鲁斯·威利斯", "hollywood"),
    "bryan cranston": ("布莱恩·科兰斯顿", "hollywood"),
    "catherine blanchett": ("凯特·布兰切特", "hollywood"),
    "christian bale": ("克里斯蒂安·贝尔", "hollywood"),
    "christoph waltz": ("克里斯托弗·瓦尔兹", "hollywood"),
    "christopher hemsworth": ("克里斯·海姆斯沃斯", "hollywood"),
    "chris hemsworth": ("克里斯·海姆斯沃斯", "hollywood"),
    "cillian murphy": ("基里安·墨菲", "hollywood"),
    "cobie smulders": ("科比·斯莫德斯", "hollywood"),
    "dwayne johnson": ("道恩·强森", "hollywood"),
    "edward norton": ("爱德华·诺顿", "hollywood"),
    "elisabeth shue": ("伊丽莎白·苏", "hollywood"),
    "elizabeth olsen": ("伊丽莎白·奥尔森", "hollywood"),
    "emily blunt": ("艾米莉·布朗特", "hollywood"),
    "emma stone": ("艾玛·斯通", "hollywood"),
    "emma watson": ("艾玛·沃森", "hollywood"),
    "erin moriarty": ("艾琳·莫里亚提", "hollywood"),
    "eva green": ("伊娃·格林", "hollywood"),
    "ewan mcgregor": ("伊万·麦克格雷戈", "hollywood"),
    "florence pugh": ("弗洛伦丝·皮尤", "hollywood"),
    "freya allan": ("弗蕾娅·艾兰", "hollywood"),
    "gary cole": ("加里·科尔", "hollywood"),
    "gigi hadid": ("吉吉·哈迪德", "influencer"),
    "harrison ford": ("哈里森·福特", "hollywood"),
    "hayden christensen": ("海登·克里斯滕森", "hollywood"),
    "heath ledger": ("希斯·莱杰", "hollywood"),
    "henry cavill": ("亨利·卡维尔", "hollywood"),
    "hugh jackman": ("休·杰克曼", "hollywood"),
    "idris elba": ("伊德里斯·艾尔巴", "hollywood"),
    "jack nicholson": ("杰克·尼科尔森", "hollywood"),
    "james carrey": ("金·凯瑞", "hollywood"),
    "jim carrey": ("金·凯瑞", "hollywood"),
    "james mcavoy": ("詹姆斯·麦卡沃伊", "hollywood"),
    "james varney": ("詹姆斯·瓦尼", "hollywood"),
    "jim varney": ("詹姆斯·瓦尼", "hollywood"),
    "jason momoa": ("杰森·莫玛", "hollywood"),
    "jason statham": ("杰森·斯坦森", "hollywood"),
    "jennifer connelly": ("珍妮弗·康纳利", "hollywood"),
    "karl urban": ("卡尔·厄本", "hollywood"),
    "kate beckinsale": ("凯特·贝金赛尔", "hollywood"),
    "laurence fishburne": ("劳伦斯·菲什伯恩", "hollywood"),
    "fishburne": ("劳伦斯·菲什伯恩", "hollywood"),
    "lili reinhart": ("莉莉·莱因哈特", "hollywood"),
    "liam neeson": ("连姆·尼森", "hollywood"),
    "luke evans": ("卢克·伊万斯", "hollywood"),
    "lucy liu": ("刘玉玲", "asian"),
    "mads mikkelsen": ("麦斯·米科尔森", "hollywood"),
    "margaret qualley": ("玛格丽特·库里", "hollywood"),
    "mary winstead": ("玛丽·伊丽莎白·温斯特德", "hollywood"),
    "melina juergens": ("梅琳娜·尤根斯", "hollywood"),
    "michael fassbender": ("迈克尔·法斯宾德", "hollywood"),
    "michael fox": ("迈克尔·J·福克斯", "hollywood"),
    "millie bobby brown": ("米莉·波比·布朗", "hollywood"),
    "morgan freeman": ("摩根·弗里曼", "hollywood"),
    "patrick stewart": ("帕特里克·斯图尔特", "hollywood"),
    "rachel weisz": ("蕾切尔·薇兹", "hollywood"),
    "rebecca ferguson": ("丽贝卡·弗格森", "hollywood"),
    "scarlett johansson": ("斯嘉丽·约翰逊", "hollywood"),
    "seth macfarlane": ("塞斯·麦克法兰", "hollywood"),
    "shannen doherty": ("珊南·道赫提", "hollywood"),
    "thomas cruise": ("汤姆·克鲁斯", "hollywood"),
    "tom cruise": ("汤姆·克鲁斯", "hollywood"),
    "thomas hanks": ("汤姆·汉克斯", "hollywood"),
    "tom hanks": ("汤姆·汉克斯", "hollywood"),
    "william murray": ("比尔·默瑞", "hollywood"),
    "bill murray": ("比尔·默瑞", "hollywood"),
    "zoe saldana": ("佐伊·索尔达娜", "hollywood"),
    "timothy dalton": ("蒂莫西·道尔顿", "hollywood"),
    "gal gadot": ("盖尔·加朵", "hollywood"),
    "emilia clarke": ("艾米莉亚·克拉克", "hollywood"),
    "ana de armas": ("安娜·德·阿玛斯", "hollywood"),
    "anakin skywalker": ("阿纳金·天行者", "character"),
    "homelander": ("祖国人", "character"),
    "palpatine": ("帕尔帕廷", "character"),
    "saul goodman": ("索尔·古德曼", "character"),
    "george clooney": ("乔治·克鲁尼", "hollywood"),
    "eminem": ("埃米纳姆", "music"),
    "will smith": ("威尔·史密斯", "hollywood"),
    "jean claude van damme": ("尚格云顿", "hollywood"),
    "jcvd": ("尚格云顿", "hollywood"),
    "sydney sweeney": ("茜德妮·斯威尼", "hollywood"),
    "natalia dyer": ("娜塔莉娅·戴尔", "hollywood"),
    "anya taylor joy": ("安雅·泰勒-乔伊", "hollywood"),
    "elvis presley": ("猫王", "music"),
    "joe rogan": ("乔·罗根", "influencer"),
    # P2: 根目录游离 .dfm 归队（DeepFaceLive 官方公开模型包，此前散在库外加载不到）
    "jackie chan": ("成龙", "asian"),
    "keanu reeves": ("基努·里维斯", "hollywood"),
    "bryan greynolds": ("布莱恩·格雷诺兹（DFL 官方演示脸）", "character"),  # 官方合成人格，合规最干净
    # 亚洲面孔（RTM 补充，含华语/韩国明星）
    "gao yuanyuan": ("高圆圆", "asian"),
    "zhao jinmai": ("赵今麦", "asian"),
    "tong liya": ("佟丽娅", "asian"),
    "tongtong yang": ("杨童童", "asian"),
    "yang tongtong": ("杨童童", "asian"),
    "huang yi": ("黄奕", "asian"),
    "daniel wu": ("吴彦祖", "asian"),
    "sha yi": ("沙溢", "asian"),
    "chen shu": ("陈数", "asian"),
    "zhang zi feng": ("张子枫", "asian"),
    "lee sun kyun": ("李善均", "asian"),
    "jisoo": ("Jisoo (BLACKPINK)", "asian"),
    "karina": ("Karina (aespa)", "asian"),
    "blackpink": ("BLACKPINK", "asian"),
    "gulnazar": ("古力娜扎", "asian"),
    "mulan": ("花木兰", "character"),
}

_STOP = {"320", "384", "256", "224", "288", "352", "416", "448", "512", "res", "wf", "df",
         "saehd", "liae", "udt", "utd", "gan", "amp", "by", "the", "high", "dims", "new",
         "old", "final", "test", "model", "head", "morph", "v1", "v2", "mini", "rtm", "k",
         "another", "shadhighlight", "la", "land", "reacher", "ciri", "highgan", "minirtm",
         "dfm", "tdeepr", "me", "obstruction", "cxsmo", "selftrained"}

# S7 文件名级人工裁定（循证梳理后的最终真相，优先级最高）：
#   这些文件名既不含可解析的名人 token，也无括号注名——自动解析必然落 other/caution 且中文名难看。
#   逐个查证后钉死正名+分类；等级仍走 category 规则（influencer→caution 不放行）。
FILE_OVERRIDES = {
    "TheNicoleT_288.dfm":       ("TheNicoleT", "influencer"),          # Twitch 主播，素人级
    "Angelicatrae_288.dfm":     ("Angelica Trae", "influencer"),       # 社媒网红，素人级
    "Eden_Cher_TdeepR_384.dfm": ("Eden Cher", "influencer"),           # 社区订制素人
    "Jasmin_Obstruction_ME_320_Res_by_Cxsmo.dfm": ("Jasmin（社区素人）", "influencer"),
    "MicaSuarez_320.dfm":       ("米卡·苏亚雷斯", "influencer"),        # 阿根廷 YouTuber
    "AVATAR_200K_320_.dfm":     ("阿凡达（纳美人）", "character"),       # 蓝脸科幻角色：真人直播必穿帮，仅本地留档
    "andras_arato_384.dfm":     ("安德拉斯·阿拉托（Hide the Pain Harold）", "influencer"),
}


def norm_key(fname: str) -> str:
    base = Path(fname).stem
    base = re.sub(r"\(.*?\)", " ", base)
    base = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", base)   # S7: 驼峰粘连拆词（JasonMomoa→Jason Momoa）
    toks = re.split(r"[^A-Za-z]+", base.lower())
    toks = [t for t in toks if len(t) >= 2 and t not in _STOP]
    return " ".join(toks)


def _paren_key(fname: str) -> str:
    """S7: 括号注名优先——社区惯用「艺名(真名)」（EmiliAMP-V1(Emilia_Clarke)），
    旧逻辑把括号连内容一起丢，正主反而没匹配上 → 一线明星被误判 other/caution。"""
    for grp in re.findall(r"\(([^)]+)\)", Path(fname).stem):
        toks = [t for t in re.split(r"[^A-Za-z]+", grp.lower()) if len(t) >= 2 and t not in _STOP]
        k = " ".join(toks)
        if k in NAMES:
            return k
        for known in NAMES:
            if known in k:
                return known
    return ""


def resolve(fname: str):
    """→ (中文名, 分类, 合规等级)。未识别的英文名回退首字母大写 + other/caution。
    S7 匹配顺序：政治闸(主名+括号名都查) → 人工裁定 → 主名精确 → 括号注名 → 主名子串。
    括号排第 4 不排第 2：括号常是「饰演角色/训练素材」注记（Gulnazar_(Mulan)），
    主名能精确认出正主时括号只会带偏；主名认不出时（EmiliAMP(Emilia_Clarke)）括号才是救命线索。"""
    mkey, pkey = norm_key(fname), _paren_key(fname)
    for key in (mkey, pkey):
        toks = set(key.split())
        if toks and toks & {t for p in POLITICIANS for t in p.split()} and any(
                p in key or all(t in toks for t in p.split()) for p in POLITICIANS):
            cn, cat = NAMES.get(key, (key.title(), "other"))
            return cn, cat, "blocked"
    if Path(fname).name in FILE_OVERRIDES:
        cn, cat = FILE_OVERRIDES[Path(fname).name]
        return cn, cat, "ok" if cat in ("hollywood", "asian", "music", "character") else "caution"
    cn = cat = None
    if mkey in NAMES:
        cn, cat = NAMES[mkey]
    elif pkey:
        cn, cat = NAMES[pkey]
    else:
        for k, (c, ct) in NAMES.items():
            if k in mkey or mkey in k:
                cn, cat = c, ct; break
    if cn is None:
        cn, cat = mkey.title(), "other"
    level = "ok" if cat in ("hollywood", "asian", "music", "character") else "caution"
    return cn, cat, level


def build():
    report = {}
    if REPORT.exists():
        rj = json.loads(REPORT.read_text(encoding="utf-8"))
        for m in rj.get("models", []):
            report[m["model"]] = m
    # S7: 保留旧表的运行期字段（live_ok/gpu_swap_ms/probed_on…由 .104 实测探活写入，
    #   report 里没有）——旧逻辑整表重写会把它们抹掉，S6 之后 build() 一跑探活史就归零。
    prev = {}
    if REGISTRY_OUT.exists():
        try:
            prev = {e["file"]: e for e in
                    json.loads(REGISTRY_OUT.read_text(encoding="utf-8")).get("entries", [])}
        except Exception:
            prev = {}
    entries = []
    for mp in sorted(COMMUNITY.rglob("*.dfm")):
        cn, cat, level = resolve(mp.name)
        met = report.get(mp.name, {})
        thumb = THUMBS / (mp.stem + ".jpg")
        e = {
            "file": mp.name,
            "path": str(mp.relative_to(BASE)),
            "cn": cn, "category": cat, "compliance": level,
            "res": met.get("input"),
            "morphable": met.get("morphable", False),
            "self_id": met.get("self_id"), "id_shift": met.get("id_shift"),
            "det_rate": met.get("det_rate"),
            "verify_ok": met.get("ok"),
            "thumb": str(thumb.relative_to(BASE)) if thumb.exists() else None,
        }
        for k, v in (prev.get(mp.name) or {}).items():
            if k not in e:
                e[k] = v                      # 运行期字段原样带过来
        entries.append(e)
    REGISTRY_OUT.write_text(json.dumps({"n": len(entries), "entries": entries},
                                       ensure_ascii=False, indent=2), encoding="utf-8")
    return entries


if __name__ == "__main__":
    es = build()
    from collections import Counter
    print(f"[registry] {len(es)} 个角色 → {REGISTRY_OUT}")
    print("  分类:", dict(Counter(e["category"] for e in es)))
    print("  合规:", dict(Counter(e["compliance"] for e in es)))
    print("  有缩略图:", sum(1 for e in es if e["thumb"]), " 体检通过:", sum(1 for e in es if e["verify_ok"]))
