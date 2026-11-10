use serde_json::Value;

#[test]
fn frozen_gtts_unicode_tokenizer_contract() {
    let fixture: Value = serde_json::from_str(include_str!("fixtures/python-providers.json")).unwrap();
    for case in fixture["tokenizer"].as_array().unwrap() {
        let actual = tts_providers::google::tokenize(case["text"].as_str().unwrap()).unwrap();
        assert_eq!(serde_json::to_value(actual).unwrap(), case["tokens"], "text={}", case["text"]);
    }
}

#[test]
fn frozen_edge_v2_tokens_and_google_rpc() {
    let fixture: Value = serde_json::from_str(include_str!("fixtures/python-providers.json")).unwrap();
    for case in fixture["edge_tokens"].as_array().unwrap() {
        assert_eq!(tts_providers::edge::token_at_unix(case["unix"].as_u64().unwrap()), case["token"].as_str().unwrap());
    }
    for case in fixture["edge_ssml"].as_array().unwrap() {
        let request: tts_providers::ProviderRequest = serde_json::from_value(case["request"].clone()).unwrap();
        let parts = tts_providers::edge::text_parts(&request.text, 4096);
        assert_eq!(parts.len(), 1);
        assert_eq!(tts_providers::edge::ssml(&request, &parts[0]).unwrap(), case["ssml"].as_str().unwrap());
    }
    for case in fixture["rpc"].as_array().unwrap() {
        let body = tts_providers::google::rpc_body(case["text"].as_str().unwrap(), case["language"].as_str().unwrap()).unwrap();
        let decoded = url::form_urlencoded::parse(body.as_bytes()).find(|(key, _)| key == "f.req").unwrap().1.into_owned();
        let actual: Value = serde_json::from_str(&decoded).unwrap();
        let expected = &case["rpc"];
        assert_eq!(actual[0][0][0], expected[0][0][0]);
        let actual_args: Value = serde_json::from_str(actual[0][0][1].as_str().unwrap()).unwrap();
        let expected_args: Value = serde_json::from_str(expected[0][0][1].as_str().unwrap()).unwrap();
        assert_eq!(actual_args, expected_args);
    }
}
