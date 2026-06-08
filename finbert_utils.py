from transformers import AutoTokenizer, AutoModelForSequenceClassification
import torch
from typing import Tuple

device = "cuda:0" if torch.cuda.is_available() else "cpu"

tokenizer = AutoTokenizer.from_pretrained("ProsusAI/finbert")
model = AutoModelForSequenceClassification.from_pretrained("ProsusAI/finbert").to(device)
labels = ["positive", "negative", "neutral"]

def estimate_sentiment(news):
    if news:
        tokens = tokenizer(news, return_tensors="pt", padding=True).to(device)
        
        result = model(tokens["input_ids"], attention_mask=tokens["attention_mask"])["logits"]
        result = torch.nn.functional.softmax(torch.sum(result, 0), dim=-1)
        
        # .item() pulls the clean number out of the PyTorch tensor
        probability = result[torch.argmax(result)].item()
        sentiment = labels[torch.argmax(result).item()]
        
        return probability, sentiment
    else:
        return 0, labels[-1]

# Pushed completely to the left so Python knows it's the main execution block
if __name__ == "__main__":
    test_headlines = [
        'The company reported a significant increase in revenue this quarter.', 
        'The CEO announced a new strategic partnership with a major industry player.', 
        'Market responded positively to the company\'s latest product launch, driving up stock prices.', 
        'The market responded positvely to the news!'
    ]
    
    prob, sentiment = estimate_sentiment(test_headlines)
    
    print("\n--- AI SENTIMENT TEST ---")
    print(f"Overall Sentiment: {sentiment.upper()}")
    print(f"AI Confidence:     {prob * 100:.2f}%")
    print(f"Using GPU/CUDA?    {torch.cuda.is_available()}")
    print("-------------------------\n")