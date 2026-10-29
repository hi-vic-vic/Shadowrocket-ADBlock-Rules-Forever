# Shadowrocket 黑名单 / 白名单 + 去广告

这是 [johnshall/Shadowrocket-ADBlock-Rules-Forever](https://github.com/johnshall/Shadowrocket-ADBlock-Rules-Forever) 的 fork，只保留两份配置：

- **黑名单规则 + 去广告**：不确定的网站直连，被墙的走代理
- **白名单规则 + 去广告**：不确定的网站走代理，top500 里可直连的网站和中国网站直连

代理和直连规则每天从上游 `release` 分支的 `sr_top500_banlist_ad.conf`、`sr_top500_whitelist_ad.conf` 原样取来。上游自带的内联广告段会被去掉，换成下面这份合并名单。

广告来源只有：

- [blackmatrix7 Advertising](https://github.com/blackmatrix7/ios_rule_script/tree/master/rule/Shadowrocket/Advertising) 的域名，以及 `Advertising.list` 里的 IP-CIDR 和锚定到域名的 URL-REGEX
- [anti-AD](https://github.com/privacy-protection-tools/anti-AD) 的 `anti-ad-surge2.txt`

合并时去掉重复域名，以及已经被更短父后缀盖住的域名。blackmatrix7 的 DOMAIN-KEYWORD 不进入规则。`custom/ad-allow.list` 用来放行，`custom/ad-block.list` 用来追加拦截。放行域名如果仍被更短的父后缀盖住，配置会在 DOMAIN-SET 之前自动加上一条 DIRECT 引用。

GitHub Actions 每天北京时间 08:00 运行 **Merge ad-block rules**，也可以手动运行。内容没有变化时不会提交。

## 订阅

在 Shadowrocket 底部打开 **配置**，点右上角 **+**，把地址粘贴到 URL，点 **下载**，再点选这份配置。已经连着的话，断开再连一次。

黑名单规则 + 去广告：

```
https://raw.githubusercontent.com/hi-vic-vic/Shadowrocket-ADBlock-Rules-Forever/release/sr_top500_banlist_ad.conf
```

白名单规则 + 去广告：

```
https://raw.githubusercontent.com/hi-vic-vic/Shadowrocket-ADBlock-Rules-Forever/release/sr_top500_whitelist_ad.conf
```

两份配置里的广告段都是：

```
DOMAIN-SET,https://raw.githubusercontent.com/hi-vic-vic/Shadowrocket-ADBlock-Rules-Forever/release/dist/ad-domain.list,REJECT
RULE-SET,https://raw.githubusercontent.com/hi-vic-vic/Shadowrocket-ADBlock-Rules-Forever/release/dist/ad-rules.list,REJECT
```

黑名单里，这两行在手工 Proxy 之后、其余代理规则之前。白名单里，这两行在直连/代理规则之前。只想把广告加进自己的配置时，把这两行贴进 `[Rule]`，策略选 **REJECT**。

域名名单：

```
https://raw.githubusercontent.com/hi-vic-vic/Shadowrocket-ADBlock-Rules-Forever/release/dist/ad-domain.list
```

IP 和 URL-REGEX：

```
https://raw.githubusercontent.com/hi-vic-vic/Shadowrocket-ADBlock-Rules-Forever/release/dist/ad-rules.list
```

HTTPS 上的 URL-REGEX 需要打开 MITM 才能看到路径。
