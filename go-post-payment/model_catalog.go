// Extracted from xupay/internal/cslive/model_catalog.go; see SOURCE.json.
package main

import ()

type model_planDefinition struct {
	id, label, checkout, hosted string
	team                        bool
	subscriptionTypes           map[string]bool
}

// 套餐能力与结算映射共享一份定义，避免调用方与执行路径各自维护名单。
var model_planDefinitions = []model_planDefinition{
	{"go", "Go", "chatgptgoplan", "go", false, protocol_StringSet("go", "go_plan", "chatgptgo", "chatgptgoplan")},
	{"plus", "Plus", "chatgptplusplan", "basic", false, protocol_StringSet("basic", "plus", "basic_plan", "chatgptplusplan")},
	{"pro5x", "Pro 5X", "chatgptprolite", "standard", false, protocol_StringSet("standard", "prolite", "standard_plan", "pro5", "pro5x", "chatgptprolite")},
	{"pro20x", "Pro 20X", "chatgptpro", "premium", false, protocol_StringSet("premium", "pro", "premium_plan", "pro20", "pro20x", "chatgptpro")},
	{"promax", "Pro Max", "chatgptpromax", "promax", false, protocol_StringSet("promax", "promax_plan", "pro_max", "pro_max_plan", "chatgptpromax")},
	{"team", "Team", "chatgptteamplan", "team", true, protocol_StringSet("team", "team_plan", "chatgptteam", "chatgptteamplan", "default")},
	{"team5x", "Team 5X", "chatgptteamplan", "team", true, protocol_StringSet("team5x", "team_5x", "prolite", "chatgptprolite", "chatgptteamplan", "team")},
}

func model_findPlan(id string) model_planDefinition {
	for _, plan := range model_planDefinitions {
		if plan.id == id {
			return plan
		}
	}
	return model_planDefinition{}
}

// MatchesSubscriptionType 只匹配上游响应值，不把响应别名用作 SDK 套餐 ID。
func model_MatchesSubscriptionType(plan, value string) bool {
	definition := model_findPlan(plan)
	return definition.subscriptionTypes[value]
}
