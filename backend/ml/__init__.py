"""The two models SpeakQL trains, and the data they are trained on.

    spider_prep.py     Spider 1.0, prepared: questions, gold SQL, schemas
    synth.py           synthetic pairs over THIS warehouse, each one verified
                       by running it
    retriever_train.py Model A -- MiniLM bi-encoder, schema retrieval
    retriever_eval.py  the recall table, against the lexical baseline
    generator_train.py Model B -- CodeT5-small, two stages
    generator_eval.py  exact match and execution accuracy

Nothing in here is imported by the API at request time. The API loads the
trained checkpoints through core/schema_retriever.py and core/sql_generator.py,
and works without them -- lexical retrieval and Gemma -- which is what keeps a
failed training run from taking the product down with it.
"""
