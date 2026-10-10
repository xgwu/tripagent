#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
腾讯云安全组：为 CVM 实例追加「入站放行」规则（追加式，不触碰存量规则）。

设计要点：
  * 用 CreateSecurityGroupPolicies（**追加**），不用 Replace/Modify ⇒ 存量规则零风险。
  * 默认 **dry-run 只打印**；必须显式加 `--apply` 才真正下发。
  * 幂等：已存在完全相同的 ACCEPT 规则则跳过。
  * 凭据只从环境变量读，绝不落盘、绝不入库：
        TENCENTCLOUD_SECRET_ID / TENCENTCLOUD_SECRET_KEY
        TENCENTCLOUD_TOKEN   （可选，STS 临时凭证）
        TENCENTCLOUD_REGION  （可选，默认 ap-shanghai）

用法：
  export TENCENTCLOUD_SECRET_ID=... TENCENTCLOUD_SECRET_KEY=...
  python apply_sg_rules.py --instance ins-c3uf08z1 --add TCP:80,TCP:3000            # 预演
  python apply_sg_rules.py --instance ins-c3uf08z1 --add TCP:80,TCP:3000 --apply    # 执行

  # 直接指定安全组、只放行「我当前公网 IP」：
  python apply_sg_rules.py --sg sg-xxxx --add TCP:80 --source MY_PUBLIC_IP --apply
"""
import argparse
import os
import sys

from tencentcloud.common import credential
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile
from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
from tencentcloud.vpc.v20170312 import vpc_client, models as vpc_models
from tencentcloud.cvm.v20170312 import cvm_client, models as cvm_models


def build_clients(region):
    sid = os.environ.get("TENCENTCLOUD_SECRET_ID")
    skey = os.environ.get("TENCENTCLOUD_SECRET_KEY")
    tok = os.environ.get("TENCENTCLOUD_TOKEN") or None
    if not sid or not skey:
        sys.exit("✗ 缺少环境变量 TENCENTCLOUD_SECRET_ID / TENCENTCLOUD_SECRET_KEY")
    cred = credential.Credential(sid, skey, tok) if tok else credential.Credential(sid, skey)
    http = HttpProfile()
    http.endpoint = "vpc.tencentcloudapi.com"
    cp = ClientProfile(); cp.httpProfile = http
    vpcc = vpc_client.VpcClient(cred, region, cp)
    http2 = HttpProfile()
    http2.endpoint = "cvm.tencentcloudapi.com"
    cp2 = ClientProfile(); cp2.httpProfile = http2
    cvmc = cvm_client.CvmClient(cred, region, cp2)
    return vpcc, cvmc


def resolve_sg_ids(vpcc, cvmc, instance_id, sg_arg):
    """返回待处理的安全组 ID 列表。"""
    if sg_arg:
        return [s.strip() for s in sg_arg.split(",") if s.strip()]
    if not instance_id:
        sys.exit("✗ 需指定 --instance 或 --sg 之一")
    req = cvm_models.DescribeInstancesRequest()
    req.InstanceIds = [instance_id]
    try:
        resp = cvmc.DescribeInstances(req)
    except TencentCloudSDKException as e:
        sys.exit(f"✗ DescribeInstances 失败：{e}")
    if not resp.InstanceSet:
        sys.exit(f"✗ 未找到实例 {instance_id}（region 是否正确？）")
    ins = resp.InstanceSet[0]
    print(f"  实例 {ins.InstanceId}  名称={getattr(ins,'InstanceName','') or '-'}  地域={getattr(ins,'Placement',None).Zone if getattr(ins,'Placement',None) else '-'}")
    return list(ins.SecurityGroupIds or [])


def get_policies(vpcc, sg_id):
    req = vpc_models.DescribeSecurityGroupPoliciesRequest()
    req.SecurityGroupId = sg_id
    resp = vpcc.DescribeSecurityGroupPolicies(req)
    ps = resp.SecurityGroupPolicySet
    return ps.Version, (ps.Ingress or []), (ps.Egress or [])


def parse_add(spec):
    out = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" not in item:
            sys.exit(f"✗ --add 格式应为 协议:端口，如 TCP:80（收到 {item!r}）")
        proto, port = item.split(":", 1)
        out.append((proto.strip().upper(), port.strip()))
    return out


def rule_exists_ingress(ingress, proto, port, source):
    for p in ingress:
        if (p.Protocol or "").upper() == proto and str(p.Port) == port \
           and (p.CidrBlock or "") == source and (p.Action or "").upper() == "ACCEPT":
            return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance", default=os.environ.get("TA_INSTANCE_ID", "ins-c3uf08z1"))
    ap.add_argument("--sg", default="", help="直接指定安全组 ID（可逗号分隔），给了就不查实例")
    ap.add_argument("--add", required=True, help="要追加的入站放行，如 TCP:80,TCP:3000")
    ap.add_argument("--source", default="0.0.0.0/0", help="来源网段；可填 MY_PUBLIC_IP 表示发起请求的公网 IP")
    ap.add_argument("--desc", default="TripAgent web (added by apply_sg_rules.py)")
    ap.add_argument("--region", default=os.environ.get("TENCENTCLOUD_REGION", "ap-shanghai"))
    ap.add_argument("--apply", action="store_true", help="真正下发（不加则只预演）")
    args = ap.parse_args()

    want = parse_add(args.add)
    print(f"== 地域 {args.region}　模式 {'APPLY（真改）' if args.apply else 'DRY-RUN（只打印）'} ==")
    vpcc, cvmc = build_clients(args.region)

    sg_ids = resolve_sg_ids(vpcc, cvmc, args.instance, args.sg)
    if not sg_ids:
        sys.exit("✗ 实例未绑定任何安全组")
    print(f"  安全组：{sg_ids}")

    for sg in sg_ids:
        try:
            version, ingress, egress = get_policies(vpcc, sg)
        except TencentCloudSDKException as e:
            print(f"  [{sg}] ✗ DescribeSecurityGroupPolicies 失败：{e}")
            continue
        print(f"\n--- 安全组 {sg}（当前 Version={version}，入站 {len(ingress)} 条 / 出站 {len(egress)} 条）---")
        for p in ingress:
            print(f"    入站 P{p.PolicyIndex:<3} {p.Protocol:<5} {str(p.Port):<12} {p.CidrBlock or p.Ipv6CidrBlock or '':<20} {p.Action}")

        to_add = []
        for proto, port in want:
            if rule_exists_ingress(ingress, proto, port, args.source):
                print(f"    = 已存在，跳过：{proto}:{port} <- {args.source}")
            else:
                to_add.append((proto, port))

        if not to_add:
            print("    ✓ 无需变更")
            continue

        policies = []
        for proto, port in to_add:
            p = vpc_models.SecurityGroupPolicy()
            p.Protocol = proto
            p.Port = port
            p.CidrBlock = args.source
            p.Action = "ACCEPT"
            p.PolicyDescription = args.desc
            policies.append(p)
            print(f"    + 将追加：{proto}:{port} <- {args.source}  ACCEPT  ({args.desc})")

        if not args.apply:
            print("    (预演，未下发；加 --apply 执行)")
            continue

        setobj = vpc_models.SecurityGroupPolicySet()
        setobj.Ingress = policies          # 只加入站；不碰出站
        req = vpc_models.CreateSecurityGroupPoliciesRequest()
        req.SecurityGroupId = sg
        req.SecurityGroupPolicySet = setobj
        try:
            resp = vpcc.CreateSecurityGroupPolicies(req)
            print(f"    ✅ 已下发（RequestId={resp.RequestId}）")
        except TencentCloudSDKException as e:
            print(f"    ✗ 下发失败：{e}")

    print("\n== 完成 ==")


if __name__ == "__main__":
    main()
