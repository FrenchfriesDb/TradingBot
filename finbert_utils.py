from typing import Tuple

# Model is loaded on first call, not at import time.
# This shaves 20-30 seconds off bot startup.
_tokenizer = None
_model = None
_device = None
_labels = ["positive", "negative", "neutral"]


def _load_model():
    global _tokenizer, _model, _device
    if _model is None:
        from transformers import AutoTokenizer, AutoModelForSequenceClassification
        import torch
        _device = "cuda:0" if torch.cuda.is_available() else "cpu"
        print("Loading FinBERT model...")
        _tokenizer = AutoTokenizer.from_pretrained("ProsusAI/finbert")
        _model = AutoModelForSequenceClassification.from_pretrained("ProsusAI/finbert").to(_device)
        print("FinBERT ready.")


def estimate_sentiment(news) -> Tuple[float, str]:
    if not news:
        return 0.0, _labels[-1]

    _load_model()

    import torch
    tokens = _tokenizer(news, return_tensors="pt", padding=True).to(_device)
    result = _model(tokens["input_ids"], attention_mask=tokens["attention_mask"])["logits"]
    result = torch.nn.functional.softmax(torch.sum(result, 0), dim=-1)
    probability = result[torch.argmax(result)].item()
    sentiment = _labels[torch.argmax(result).item()]
    return probability, sentiment


if __name__ == "__main__":
    test_headlines = [
        "The company reported a significant increase in revenue this quarter.",
        "Market responded positively to the company's latest product launch.",
    ]
    prob, sentiment = estimate_sentiment(test_headlines)
    print(f"Sentiment: {sentiment.upper()} ({prob*100:.2f}%)")
