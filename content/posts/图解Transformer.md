---
date: 2026-07-26T22:55:15+08:00
title: 图解Transformer
tags: [transformer,llm]
categories: [llm]
draft: false
repost: https://jalammar.github.io/illustrated-transformer/
---

## 译者前言

本文基于 Gemini 3.6 Flash 进行翻译，并补充了一些帮助理解的知识，使用引用框框起来。

## 宏观视角

首先，我们将整个模型视为一个黑盒。在机器翻译任务中，输入某种语言的一句话，它就能输出对应的另一种语言译文。

![img](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/the_transformer_3.png)

展开这个“擎天柱”般精妙的内部结构，我们可以看到它主要由编码器（encoding component）、解码器（decoding component）以及两者之间的连接构成。

![img](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/The_transformer_encoders_decoders.png)

编码组件由几个编码器（encoder）堆叠而成（原论文中将 6 个编码器叠在了一起——6 这个数字并没有什么魔力，你完全可以尝试其他的堆叠数量）。解码组件则是相同数量的解码器（decoder）堆叠而成。

![img](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/The_transformer_encoder_decoder_stack.png)

所有编码器在结构上完全相同（但它们 **并不共享权重**）。每个编码器都可以拆分为两个子层：

![img](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/Transformer_encoder.png)

1. **自注意力层（Self-Attention Layer）**：编码器的输入首先会流经该层。在对某个特定单词进行编码时，自注意力层能够帮助编码器关注输入句子中的其他单词。我们将在文章后半部分对自注意力机制进行更深入的探讨。
2. **前馈神经网络（FFNN, Feed-Forward Neural Network）**：自注意力层的输出会被送入前馈神经网络。完全相同的前馈网络会被独立地应用到每个位置上。

解码器同样包含这两个子层，但在它们之间多了一个 **注意力层**（即交叉注意力层）。这一层的作用是帮助解码器聚焦于输入句子中相关的部分（类似于传统 [seq2seq 模型](https://jalammar.github.io/visualizing-neural-machine-translation-mechanics-of-seq2seq-models-with-attention/) 中注意力机制的作用）。

![img](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/Transformer_decoder.png)

## 引入张量（Tensors）

既然我们已经了解了模型的主要组件，接下来就让我们看看各种向量/张量是如何在这些组件之间流转，并最终将已训练模型的输入转化为输出的。

与一般的 NLP（自然语言处理）应用一样，我们首先会使用 [词嵌入（embedding）算法](https://medium.com/deeper-learning/glossary-of-deep-learning-word-embedding-f90c3cec34ca)，将每个输入的单词转化为一个向量。

![每个单词都被嵌入到一个大小为 512 的向量中。我们将用这些简单的方框来表示这些向量。](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/embeddings.png)

这种词嵌入操作只发生在最底层的编码器中。对于所有的编码器来说，它们都有一个共同的特性：都会接收一个向量列表，且其中每个向量的维度大小均为 512。在最底层的编码器里，接收到的就是词嵌入向量；而在其他的编码器里，接收到的则是其正下方那个编码器的输出。这个列表的大小（长度）是一个我们可以设置的超参数——通常情况下，它就是我们训练数据集中最长句子的长度。

在对输入序列中的单词完成词嵌入后，每个词向量都会依次流经编码器的这两个子层。

![img](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/encoder_with_tensors.png)

> 示意图：
>
> ```
> 输入句子:        "Thinking"      "Machines"
>                        ↓               ↓
> Tokenizer:         101             3241        (映射为 Token ID)
>                        ↓               ↓
> Embedding 层:    [词嵌入1]        [词嵌入2]      (查表映射为 512 维向量)
>                     +               +
> Positional Enc:  [位置向量1]      [位置向量2]    (加上位置编码)
>                     ↓               ↓
> ------------------ 进入 Encoder 堆叠 ------------------
> 底层 Encoder:    [输入向量1]      [输入向量2] ... (补齐到 Seq_Len) --> 输入维度: (Seq_Len, 512)
>                     ↓               ↓
> 中层 Encoder:    [特征向量1]      [特征向量2] ...                  --> 输入维度: (Seq_Len, 512)
>                     ↓               ↓
> 顶层 Encoder:    [高阶向量1]      [高阶向量2] ...                  --> 输入维度: (Seq_Len, 512)
> ```
>
> 其中说的 "向量" 指的是 token 经过 embedding 层之后（可能再加上位置向量）得到的 512 维向量，而 Seq_Len 就是指向量列表大小，一般是训练时最长的句子经过 embedding 层后得到的向量个数。

到这里，我们开始能够看到 Transformer 的一个关键特性：**每个位置上的单词在编码器中都是沿着各自独立的路径流转的**。

在自注意力层中，这些路径之间存在依赖关系（在后面讲到计算过程的时候会看到）；然而，在随后的前馈神经网络层中，这种依赖关系就不复存在了。因此，各个路径在流经前馈层时可以 **并行执行**。

接下来，我们将用一个更短的输入句子作为示例，深入探究编码器的每个子层内部究竟发生了什么。

## 开始编码！

正如前文所述，编码器接收一个向量列表作为输入。它处理该列表的过程是：首先将这些向量送入“自注意力”层，接着传入前馈神经网络，最后将输出向上传递给下一个编码器。

![每个位置的单词都会经过一个自注意力过程。然后，它们各自进入一个前馈神经网络——是同一个网络，只是这样更清晰地表达每个向量在该网络中不具有依赖关系](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/encoder_with_tensors_2.png)

## 从宏观角度理解自注意力

不要被我随口抛出“自注意力（self-attention）”这个词给唬住了，搞得好像这是个大家理应熟知的概念似的。在我阅读《Attention is All You Need》论文之前，我自己也从未接触过这个概念。下面就让我们来梳理一下它的工作原理。

假设我们要翻译下面这句输入的句子：

> “The animal didn't cross the street because it was too tired” （这只动物没有穿过马路，因为它太累了。）

句子中的“**it**”指的是什么？是指马路（street），还是指这只动物（animal）？对人类来说这是个很简单的问题，但对算法而言可没那么简单。

当模型在处理“**it**”这个词时，自注意力机制能够帮助它将“**it**”与“**animal**”关联起来。

当模型处理每个单词（输入序列中的每个位置）时，自注意力机制允许它去查看输入序列中的其他位置，寻找能帮助对当前单词进行更好编码的线索。

如果你对循环神经网络（RNN）比较熟悉，不妨联想一下：RNN 是如何通过维护一个隐状态（hidden state），将之前已处理过的单词/向量的表示融入到当前正在处理的单词中的。而自注意力机制，正是 Transformer 用来将其他相关单词的“理解”融汇到当前正在处理的单词中的方法。

一定要去看看 [Tensor2Tensor 的 Notebook](https://colab.research.google.com/github/tensorflow/tensor2tensor/blob/master/tensor2tensor/notebooks/hello_t2t.ipynb)，你可以在那里加载一个 Transformer 模型，并使用这种交互式可视化工具来观察它的工作过程。

![img](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/transformer_self-attention_visualization.png)

## 深入理解自注意力机制

我们先来看看如何用 **向量** 计算自注意力，然后再看它在实际中是如何通过 **矩阵** 来实现的。（下面这个计算过程同时也回答了，为什么在自注意力的计算的过程中，词嵌入之间的是有依赖关系的）

**计算自注意力的第一步**，是从每个编码器的输入向量（在本例中，即每个单词的嵌入向量）创建出三个向量。因此，对于每个单词，我们都会创建一个 **Query 向量**、一个 **Key 向量** 和一个 **Value 向量**。这些向量是通过将嵌入向量乘以我们在训练过程中学习到的三个权重矩阵来生成的。

需要注意的是，这些新向量的维度比嵌入向量要小。它们的维度是 **64**，而词嵌入向量以及编码器的输入/输出向量的维度则是 **512**。它们 **并非必须** 更小，这是一种架构上的设计选择，目的是为了使多头注意力（multiheaded attention，后面会讲到）的计算量在整体上保持相对恒定。

![将 x1 乘以 WQ 权重矩阵即可得到 q1 ，即与该词关联的“查询”向量。最终，我们为输入句子中的每个词创建了一个“查询”投影、一个“键”投影和一个“值”投影](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/transformer_self_attention_vectors.png)

所以“Query”、“Key”和“Value”向量到底是什么？

它们是用于计算、以及你如何看待注意力机制的抽象概念。只要你接着往下阅读后文关于注意力的计算方式，就能基本掌握这些向量所扮演的角色了。

**计算自注意力的第二步**，是计算一个 **分值（Score）**。假设我们正在为本例中的第一个单词“Thinking”计算自注意力，我们需要拿输入句子中的每一个单词与该单词进行打分。这个分值决定了在对某个位置的单词进行编码时，要在输入句子的其他部分投入多少注意力。

分值的计算方法，是将当前单词的 **Query 向量** 与我们要打分的那个单词的 **Key 向量** 做 **点积（dot product）**。因此，如果我们正在处理位置 #1 处的单词的自注意力，第一个分值就是 $q_1$ 和 $k_1$ 的点积；第二个分值则是 $q_1$ 和 $k_2$ 的点积。

![img](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/transformer_self_attention_score.png)

**计算自注意力的第三步和第四步**，是将计算出的分值除以 8（即论文中所用 Key 向量维度的平方根 $\sqrt{64}$。这样能带来更稳定的梯度，此处也可以采用其他数值，但 8 是默认值），接着将计算结果送入 Softmax 操作中。Softmax 可以对分值进行归一化，使其全部为正数且加和为 1。

![img](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/self-attention_softmax.png)

这个经过 Softmax 处理后的分值，决定了每个单词在该位置上的表达程度（权重）。显而易见，当前位置的单词自身得分最高，但有时候对于关注相关的其他单词也是非常有用的。

**计算自注意力的第五步**，是将每个 Value 向量乘以对应的 Softmax 分值（为后续的加权求和做准备）。这里的直觉在于：保持我们想要重点关注的单词的 Value 值不受影响，同时抑制那些无关单词（通过将它们乘以极小的数值，例如 0.001）。

**计算自注意力的第六步**，是将这些加权后的 Value 向量累加求和。这就生成了自注意力层在该位置（对于第一个单词）的最终输出。

![img](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/self-attention-output.png)

自注意力的计算到这里就完成了。生成的向量可以直接传递给随后的前馈神经网络。然而在实际的代码实现中，为了提高处理速度，这一计算过程是以 **矩阵形式** 并行完成的。既然我们已经从单词粒度了解了计算的直觉逻辑，接下来就让我们看看矩阵形式的实现方式。

## 自注意力的矩阵计算形式

第一步计算 Query、Key 和 Value 矩阵。具体的实现方式是：我们将词嵌入向量打包拼接为一个矩阵 $X$，然后分别乘以我们在训练过程中学习到的权重矩阵（$W^Q, W^K, W^V$）。

![X 矩阵中的每一行都对应输入句子中的一个词。我们再次看到词嵌入向量（512，图中对应 4 个方框）与 q/k/v 向量（64，图中对应 3 个方框）的大小差异](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/self-attention-matrix-calculation.png)

最终，由于我们使用的是矩阵，可以将第二步到第六步的所有计算浓缩到一个公式中，从而直接计算出自注意力层的输出：
$$
Z = \text{softmax}\left(\frac{Q K^T}{\sqrt{d_k}}\right) V
$$
![img](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/self-attention-matrix-calculation-2.png)

## 多头注意力机制

论文通过引入一种被称为“多头”注意力（multi-headed attention）的机制，对自注意力层进行了进一步改进。这种机制从两个方面提升了注意力层的性能：

1. **扩展了模型聚焦于不同位置的能力**。在上面的示例中，$z_1$ 虽然包含了其他所有编码向量的一少部分信息，但它可能主要还是被单词本身所占据。当我们翻译像“The animal didn't cross the street because it was too tired”这样的句子时，能明确知道“it”究竟指代哪个单词会非常有帮助。
2. **为注意力层提供了多个“表示子空间（representation subspaces）”**。正如我们接下来会看到的，在多头注意力机制中，我们拥有的不仅仅是一组 Query/Key/Value 权重矩阵，而是 **多组**（Transformer 模型使用了 8 个注意力头，因此每个编码器/解码器最终拥有 8 组）。每组权重矩阵都会进行随机初始化。训练完成后，每组权重矩阵会被用来将输入的嵌入向量（或来自下层编码器/解码器的向量）投影（project）到不同的表示子空间中。

![在多头注意力机制中，我们为每个注意力头维护独立的 Q/K/V 权重矩阵，从而得到不同的 Q/K/V 矩阵。与之前一样，我们将 X 乘以 WQ/WK/WV 矩阵，得到 Q/K/V 矩阵](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/transformer_attention_heads_qkv.png)

如果我们按照上面概述的流程，使用不同的权重矩阵重复执行 8 次自注意力计算，最终就会得到 8 个不同的 $Z$ 矩阵。

![img](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/transformer_attention_heads_z.png)

但这给我们带来了一个小小的挑战：随后的前馈神经网络层并不希望接收 8 个矩阵，它需要的是 **单个矩阵**（即每个单词对应一个向量）。因此，我们需要一种方法将这 8 个矩阵压缩为一个矩阵。

我们该怎么做呢？我们将这些矩阵拼接（concat）在一起，然后乘以一个额外的权重矩阵 $W^O$。

![img](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/transformer_attention_heads_weight_matrix_o.png)

关于多头自注意力机制（multi-headed self-attention），差不多就这么多了。我明白，这里涉及到的矩阵确实挺多的。让我试着把它们全部整合到一张示意图中，方便大家在一个地方直观地查看。

![img](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/transformer_multi-headed_self-attention-recap.png)

既然我们已经接触到了注意力头，现在让我们重新审视之前的示例，看看当我们在示例句子中对单词“it”进行编码时，不同的注意力头分别聚焦在什么位置：

![当我们对“它”这个词进行编码时，一个注意力头主要集中在“动物”上，而另一个注意力头则集中在“疲倦”上——从某种意义上说，模型对“它”这个词的表征包含了“动物”和“疲倦”的一些表征](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/transformer_self-attention_visualization_2.png)

最后，如果我们把所有注意力头都加到图示中，看起来更难理解一些：

![img](https://jalammar.github.io/images/t/transformer_self-attention_visualization_3.png)

## 使用位置编码（Positional Encoding）表示序列顺序

在我们目前描述的模型中，还缺少一项重要内容：**记录输入序列中单词顺序的方法**。

为了解决这个问题，Transformer 在每个输入的嵌入向量（embedding）上都 **相加了一个向量**。这些向量遵循模型所能学习的某种特定模式，从而帮助模型确定每个单词的位置，或者序列中不同单词之间的距离。

这里的直觉在于：将这些位置数值加到嵌入向量中后，一旦它们被投影为 $Q/K/V$ 向量并在点积注意力（dot-product attention）计算时，就能为嵌入向量之间提供有意义的距离度量。

![img](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/transformer_positional_encoding_vectors.png)

如果我们假设嵌入向量（embedding）的维度大小为 4，那么实际的位置编码向量看起来就会像这样：

![img](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/transformer_positional_encoding_example.png)


这种模式（pattern）看起来会是什么样的呢？

在下图中，每一行都对应着一个向量的位置编码。因此，第一行就是我们要加到输入序列中第一个单词嵌入向量上的那个位置向量。每一行都包含 512 个数值，每个数值的大小都在 -1 到 1 之间。我们对其进行了色彩编码，以便这种规律能够直观可见。

![这是一个包含 20 个词（行）且词嵌入大小为 512（列）的位置编码示例。可以看到，它似乎被中间一分为二。这是因为左半部分的值由一个函数（使用正弦函数）生成，右半部分的值由另一个函数（使用余弦函数）生成。然后，它们被连接起来形成每个位置编码向量](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/transformer_positional_encoding_large_example.png)

> 我觉得很有必要解释一下这张图背后代表的含义。
>
> 在之前的介绍中，我们已经了解 self-attention 的计算过程是不包含 token 与 token 之间的位置信息的，比如“我 爱 你”和“你 爱 我”的计算在本质上没有区别，都是把一句话里所有的 token **同时** 抛进一个大池子里，算它们两两之间的关系。直接丢失了语序的概念。
>
> 而我们也知道，词嵌入本身是不具备位置信息的，某个词在句子 1 中或者在句子 2 中，这个词的词嵌入向量都是一样的。**因此位置编码的目的就是要给词嵌入本身加上这个词在句子中的位置信息。**
>
> 最经典的做法是（也是本篇博客的做法）：$H^{(0)} = X + P$
>
> 其中：
>
> - $X$：词嵌入矩阵；
> - $P$：位置编码矩阵；
> - $H^{(0)}$：送入第一层 Transformer 的输入。
>
> 因此对于第 $pos$ 个 token：$h_{pos}^{(0)} = x_{pos}+p_{pos}$
>
> **那么现在的疑问变成了：P 的表达式是什么？**
>
> 2017 年论文《Attention Is All You Need》使用的是固定的正弦、余弦位置编码。
>
> 给定位置 $pos$ 和维度索引 $i$（pos 和 i 分别对应于上面色彩编码图的行和列），其位置编码 PE（对应于上面色彩编码图的颜色值）为：
>
> $PE(pos,2i) = \sin \left( \frac{pos}{10000^{2i/d_{\text{model}}}} \right)$
>
> $PE(pos,2i+1) = \cos \left( \frac{pos}{10000^{2i/d_{\text{model}}}} \right)$
>
> 其中：
>
> - $pos$：token 的位置，例如 $0,1,2,\ldots$；
> - $i$：控制不同频率；
> - $d_{\text{model}}$：模型隐藏维度；
> - 偶数维使用正弦；
> - 奇数维使用余弦。
>
> 如果隐藏维度为 $d_{\text{model}}=512$，那么一个位置向量大致是：
>
> $$
> p_{pos}= [ \sin(\omega_0pos), \cos(\omega_0pos), \sin(\omega_1pos), \cos(\omega_1pos), \dots ]
> $$
> 
>
> 对于上述这个 $p_{pos}$ 的表达式，我们知道 sin 和 cos 都是周期函数，将 $w$ 看作常量，$pos$ 看作自变量，那么当 $w$ 越大，正弦/余弦波的频率就越大，即变化越快。因此 $p_{pos}$ 的第 1 维变化频率最大，第 2 维其次....以此类推。但这样有什么用呢？
>
> 我们可以用二进制来类比，对于数字 0 到 4 的二进制编码是：
>
> ```
> 0 0 0
> 0 0 1
> 0 1 0
> 0 1 1
> 1 0 0
> ```
>
> 可以发现越高位（高维）的数字变化越慢，越低位（低维）的数字变化越快。由三位数一起共同确定了一个具体的数字。
>
> 回到 $p_{pos}$，不难发现就是借助了 sin/cos 和 w 来实现了一种编码机制（二进制也是一种编码机制，借助 0 和 1 进行编码），由这组编码来唯一地确认 token 在句子中的位置（二进制是用于确认一个具体的数字）。
>
> 那再回到 sin 和 cos 本身，为什么就选择了 sin 和 cos 呢？为什么不能只用 sin 呢？一个重要原因是：只需要通过一个矩阵乘法，它们能够方便地表示 token 在句子中的 **相对距离**。
>
> 考虑一个频率 $\omega$：
> $$
> p_{pos} = \begin{bmatrix} \sin(\omega pos)\\ \cos(\omega pos) \end{bmatrix}
> $$
> 
>
> 位置移动 $k$ 后：
> $$
> p_{pos+k} =
> \begin{bmatrix}
> \sin(\omega(pos+k))\\
> \cos(\omega(pos+k))
> \end{bmatrix}
> $$
> 根据三角恒等式：
> $$
> \sin(a+b)=\sin a\cos b+\cos a\sin b
> \\
> \cos(a+b)=\cos a\cos b-\sin a\sin b
> $$
> 因此
> $$
> p_{pos+k} =
> \begin{bmatrix}
> \cos(\omega k)&\sin(\omega k)\\
> -\sin(\omega k)&\cos(\omega k)
> \end{bmatrix}
> p_{pos}
> $$
> 这里的矩阵只依赖位移 $k$，不依赖绝对位置 $pos$。也就是说模型可以通过某种线性变换，从位置 $pos$ 的编码得到位置 $pos+k$ 的编码。
>
> 上面单纯在讨论位置编码本身。那位置编码到底如何在 self-attention 中发挥作用呢？
>
> 加入位置编码后：
> $$
> h_i = x_i+p_i
> $$
> 于是：
> $$
> \begin{aligned}
> q_i &= (x_i+p_i)W_Q \\
> k_j &= (x_j+p_j)W_K
> \end{aligned}
> $$
> 两个位置之间的 Attention 分数为：
> $$
> q_i k_j^\top
> $$
> 将其展开后得到：
> $$
> \begin{aligned}
> &(x_iW_Q)(x_jW_K)^\top \\
> &(x_iW_Q)(p_jW_K)^\top \\
> &(p_iW_Q)(x_jW_K)^\top \\
> &(p_iW_Q)(p_jW_K)^\top
> \end{aligned}
> $$
> 它们分别可以理解为：
>
> 1. 词与词的语义关系
> 2. 当前词与目标位置的关系
> 3. 当前位置与目标词的关系
> 4. 位置与位置的关系
>
> 总结：通过给词嵌入向量加上一组无需训练的、可以计算任意位置的、理论上可以外推到训练长度之外的 **位置编码 $P$**，就能使得这组词嵌入携带上了每个词在句子中的位置信息。

论文（第 3.5 节）中详细描述了位置编码的计算公式。你可以在函数 `get_timing_signal_1d()` 中查看生成位置编码的具体代码。这并不是实现位置编码的唯一可行方法，但这种方法的优势在于：它能够拓展并适应未见过的序列长度（例如，当我们要求训练好的模型去翻译一个比训练集中任何句子都长的句子时）。

**2020 年 7 月更新**：上面展示的位置编码来自于 Transformer 的 Tensor2Tensor 实现版本。论文中提出的方法略有不同，它并非直接将两种信号（正弦与余弦）进行拼接（concatenate），而是将它们交织（interweave）在一起。下图展示了交织后的形态，下面是生成该图案的代码：

![img](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/attention-is-all-you-need-positional-encoding.png)

## 残差

在继续深入之前，我们需要提一下编码器架构中的一个细节：每个编码器内部的每个子层（自注意力层、前馈神经网络层）周围都包含一个 **残差连接（residual connection）**，紧接着还会进行一步层归一化（layer-normalization）操作。

![img](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/transformer_resideual_layer_norm.png)

如果我们把与自注意力相关的向量计算以及层归一化（Layer-Norm）操作可视化出来，它看起来会是这样的：

![img](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/transformer_resideual_layer_norm_2.png)

这种结构同样适用于解码器的各个子层。如果我们将一个由 2 层编码器和 2 层解码器堆叠而成的 Transformer 进行可视化，整体结构会类似于这样：

![img](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/transformer_resideual_layer_norm_3.png)

## 解码器

既然我们已经涵盖了编码器端的大部分概念，那么解码器各个组件的工作原理我们也基本上掌握了。接下来，让我们看看它们是如何协同工作的。

编码器首先处理输入序列。顶层编码器的输出随后会被转化为一组注意力向量 **K** 和 **V**。每个解码器都会在其“编码器-解码器注意力（Encoder-Decoder Attention）”层中使用这些向量，这有助于解码器聚焦于输入序列中的适当位置：

![编码阶段完成后，我们开始解码阶段。解码阶段的每一步都会输出输出序列中的一个元素（在本例中为英文翻译句子）](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/transformer_decoding_1.gif)

接下来的步骤会不断重复这一过程，直到遇到一个特定的终止符号，表明 Transformer 解码器已经完成了输出。每一步的输出都会在下一个时间步被喂给最底层的解码器，解码器们就像编码器那样，逐层向上传递它们的解码结果。而且，就像我们对编码器输入所做的那样，我们也会对这些解码器的输入进行嵌入（Embed）并加上位置编码（Positional Encoding），以标示每个单词的位置。

![img](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/transformer_decoding_2.gif)

解码器中的自注意力层（Self-Attention Layer）与编码器中的运行方式略有不同：

在解码器中，自注意力层 **只被允许关注输出序列中较早的位置**。这是通过在自注意力计算的 Softmax 步骤之前，将未来的位置进行掩码（Masking）处理（把它们设置为 $-\infty$）来实现的。

“编码器-解码器注意力”层的工作原理与多头自注意力层非常相似，唯一的区别在于：它的 **Query 矩阵** 来自于其下方的解码器层，而 **Key 矩阵** 和 **Value 矩阵** 则取自编码器堆叠层的最终输出。

> 最后一层编码器实际上不是输出 KV，而是隐藏状态矩阵 H，然后在解码器的 cross-attetion（即原文中说的 Encoder-Decoder Attention，也叫交叉注意力） 中才把它投影成 KV：
>
> 在 Cross-Attention 中：
> $$
> \begin{aligned}
> Q_{\text{cross}} &= Z_{\text{dec}}W_Q^{\text{cross}} \\
> K_{\text{cross}} &= H_{\text{enc}}W_K^{\text{cross}} \\
> V_{\text{cross}} &= H_{\text{enc}}W_V^{\text{cross}}
> \end{aligned}
> $$
> 这里：
>
> - \(Z_{\text{dec}}\) 是 Decoder 下方子层的输出；
> - \(H_{\text{enc}}\) 是顶层 Encoder 输出；
> - 三个投影矩阵都属于当前 Decoder Layer 的 Cross-Attention。
>
> 因此可以理解为：
>
> ```
> Encoder 提供原始记忆 H_enc
> 
> Decoder 的 Cross-Attention 决定：
> - 如何把这份记忆编码成 Key
> - 如何把这份记忆编码成 Value
> - 如何把当前 Decoder 状态编码成 Query
> ```
>
> 那么给出整个解码的完整结构：
>
> ```
> 目标序列 token
>       │
>       ▼
> Token Embedding + Positional Encoding
>       │
>       ▼
> ┌──────────────────────────────┐
> │ Decoder Layer 1              │
> │                              │
> │  1. Masked Self-Attention    │
> │  2. Encoder-Decoder Attention│
> │  3. Feed Forward Network     │
> └──────────────────────────────┘
>       │
>       ▼
> ┌──────────────────────────────┐
> │ Decoder Layer 2              │
> │             ...              │
> └──────────────────────────────┘
>       │
>       ▼
> Decoder Layer N
>       │
>       ▼
> Linear 层
>       │
>       ▼
> Softmax
>       │
>       ▼
> 下一个 token 的概率
> ```
>
> Decoder 有两类输入，一类是来自于 Decoder 自身已经输出的 token，另一类是 Encoder 输出的 H，H 会被 Decoder  的每一层 cross-attention 重复使用。即每个 Decoder Layer 都能直接访问同一个顶层 Encoder 输出，但每层使用自己的 \(W_K, W_V\) 进行投影。

## 最终的线性层与 Softmax 层

解码器堆栈最终输出的是一个由浮点数组成的向量。我们该如何将它转换成一个单词呢？这正是最后的线性层（Linear layer）以及随后的 **Softmax 层** 所承担的工作。

线性层是一个简单的全连接神经网络，它将解码器堆栈所产生的向量，投影（project）成一个规模大得多的向量，这个向量被称为 **Logits 向量**。

假设我们的模型从训练数据集中学习到了 10,000 个独特的英语单词（即我们模型的“输出词汇表”）。这会让 Logits 向量的宽度达到 10,000 个单元格——每个单元格对应着一个独特单词的分数值。这就是我们解读模型经过线性层输出结果的方式。

随后的 Softmax 层会将这些分数值转化为 **概率值**（全为正数，且相加之和为 1.0）。概率最高的那一个单元格会被选中，与其对应的单词就会作为该时间步（time step）的输出结果被生成出来。

![底部是顶层解码器的输出向量。最后该向量被转换成输出的单词](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/transformer_decoder_output_softmax.png)

## 回顾训练过程

既然我们已经了解在训练好的 Transformer 模型上的前向传播过程，那么顺便了解一下模型训练的核心直觉也是非常有帮助的。

在训练期间，一个未经训练的模型会经历完全相同的前向传播过程。但由于我们是在一个带有标签的训练数据集上对其进行训练，因此我们可以将它的输出结果与实际的正确输出进行对比。

为了将其可视化，假设我们的输出词汇表仅包含 6 个单词（“a”、“am”、“i”、“thanks”、“student” 以及“`<eos>`”（‘end of sentence’ 的缩写，代表句子结束））。

![我们的模型的输出词汇表是在预处理阶段创建的，甚至在我们开始训练之前就已经创建好了](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/vocabulary.png)

一旦定义好了输出词汇表，我们就可以使用一个相同宽度的向量来表示词汇表中的每个单词。这也被称为 **独热编码（one-hot encoding）**。例如，我们可以使用以下向量来表示单词 “am”：

![例子：对输出词汇表进行独热编码](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/one-hot-vocabulary-example.png)

在回顾完这些之后，接下来让我们讨论一下模型的 **损失函数（loss function）**——这是我们在训练阶段所要优化的指标，正是它引领我们打造出一个经过充分训练且精度惊人的模型。

## 损失函数

假设我们正在训练模型。假设这是训练阶段的第一步，我们正在用一个简单的例子训练它——将 “merci” 翻译为 “thanks”。

这意味着，我们希望输出是一个指向单词 “thanks” 的概率分布。但由于模型尚未经过训练，此时很难直接输出正确的结果。

![由于模型的所有参数（权重）都是随机初始化的，因此（未经训练的）模型会生成一个概率分布，其中每个单元格/单词的权重值都是任意的。我们可以将其与实际输出进行比较，然后使用反向传播算法调整模型的所有权重，使输出更接近期望输出](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/transformer_logits_output_and_label.png)

你该如何比较两个概率分布呢？简单来说，我们可以用一个分布减去另一个分布。想了解更多细节，可以参阅交叉熵（cross-entropy）和 KL 散度（Kullback–Leibler divergence）。

但请注意，这是一个极度简化的例子。更符合实际情况的是，我们会使用比单个词更长的句子。例如——输入：“je suis étudiant”，预期输出：“i am a student”。这真正意味着，我们希望模型能够接连不断地输出一系列概率分布，其中：

- 每个概率分布都由一个宽度为 `vocab_size` 的向量表示（在我们的玩具示例中是 6，但在实际情况中通常是 30,000 或 50,000 这样的数字）
- 第一个概率分布在对应单词 “i” 的单元格处拥有最高概率
- 第二个概率分布在对应单词 “am” 的单元格处拥有最高概率
- 依此类推，直到第五个输出分布指示出 `<end of sentence>`（句子结束）符号——该符号在 10,000 个元素的词汇表中也有其对应的单元格。

![我们将针对一个示例句子，在训练示例中训练我们的模型所依据的目标概率分布](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/output_target_probability_distributions.png)

在足够大的数据集上对模型训练足够长的时长后，我们希望生成的概率分布看起来会像这样：

![希望经过训练后，模型能够输出我们期望的正确翻译。当然，这并不能真正表明该短语是否包含在训练数据集中（参见：https://www.youtube.com/watch?v = TIgfjmp-4BA。请注意，即使某个位置不太可能是该时间步的输出，它也会获得一定的概率——这是 softmax 函数的一个非常有用的特性，有助于训练过程](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/output_trained_model_probability_distributions.png)

现在，因为模型是一次生成一个输出，我们可以假设模型是从该概率分布中选择概率最高的单词，并丢弃其余的单词。这是一种可行的方法（被称为 **贪婪解码 / greedy decoding**）。

另一种方法是保留概率最高的前两个单词（例如，保留 “I” 和 “a”），然后在下一步中将模型运行两次：一次假设第一个输出位置的单词是 “I”，另一次假设第一个输出位置的单词是 “a”；接着，综合考虑位置 #1 和 #2，保留产生误差较小的那个版本。我们在位置 #2 和 #3 上重复这一过程……以此类推。这种方法被称为“束搜索（beam search）”。在我们这个例子中，`beam_size` 为 2（意味着在任何时刻，内存中都会保留两个局部假设/未完成的翻译结果），`top_beams` 也为 2（意味着我们最终将返回两条翻译结果）。这两个都是你可以去实验调整的超参数。

## 译者个人理解

### 训练完整流程

下面把训练过程完整走一遍，由于 Encoder 比较好理解，我们把重心放在 Decoder 上。

假设训练数据是“我 爱 猫”。那么目标句子就是

```
I love cats <EOS>
```

对应关系：

| Decoder 输入位置 | 模型需要预测 |
| ---------------- | ------------ |
| `<BOS>`          | `I`          |
| `I`              | `love`       |
| `love`           | `cats`       |
| `cats`           | `<EOS>`      |

首先 Encoder 来处理并得到输出 $H_{\text{enc}} \in \mathbb{R}^{1\times3\times512}$。

然后将目标句子进行词嵌入和位置编码后，得到 Decoder 的输入 $X^{(0)} \in \mathbb{R}^{1\times4\times512}$，并且经过 **masked self-attention** 处理。注意与 self-attention 不同：self-attetion 原本是：
$$
S =\frac{QK^T}{\sqrt d}
$$
假设：
$$
S =
\begin{bmatrix}
2&1&3&5\\
4&2&7&1\\
3&6&8&9\\
1&2&3&4
\end{bmatrix}
$$
其中，第一行代表 <BOS> 可以关注所有的位置，其它行也是也类似的道理。但实际上 <BOS> 不能看到后面的词，所以要加入 Mask：
$$
M =
\begin{bmatrix}
0&-\infty&-\infty&-\infty\\
0&0&-\infty&-\infty\\
0&0&0&-\infty\\
0&0&0&0
\end{bmatrix}
$$
相加得到：
$$
S'= S+M =\begin{bmatrix} 2&-\infty&-\infty&-\infty\\ 4&2&-\infty&-\infty\\ 3&6&8&-\infty\\ 1&2&3&4 \end{bmatrix}
$$
最后经过 self-attention 得到的输出矩阵形状与输入矩阵形状一样，但其含义是作为 cross-attetion 的 Q。然后与 H 进行 cross-attetion 计算，最后计算出四个单词输出并计算交叉熵损失。

为什么训练无需 KV cache。因为整个目标序列在一次前向传播中已经全部存在，并且 self-attetion 可以一次矩阵运算算出全部，并不像推理那样要一个个进行预测。

### 推理完整流程

推理与训练最大的区别就是，推理时只知道：“我 爱 猫”，不知道所谓的正确输出，因此只能逐步生成。

首先还是先 forward pass 一遍 Encoder 得到 $H_{enc}$

对于每个 Decoder Layer，可以预先计算 Cross-Attention 的 KV 并存到 cross-attention KV cache 中：
$$
\begin{aligned}
K_{\text{cross}}^{(l)} &= H_{\text{enc}}W_{K,\text{cross}}^{(l)} \\
V_{\text{cross}}^{(l)} &= H_{\text{enc}}W_{V,\text{cross}}^{(l)}
\end{aligned}
$$
它们在整个生成过程中保持不变。

然后将第一个词 <BOS> 经过 embedding 和位置编码得到 X，将其输入到 Decoder 的 self-attention，计算得到 Decoder 第一层 self-attention 的 QKV：
$$
q_{\text{BOS}}, k_{\text{BOS}}, v_{\text{BOS}}
$$
将 KV 存到该层的 self-attention KV cache 中，另外，用这 QKV 计算得到第一层 cross-attention 的 Q：
$$
q_{cross,0}=\operatorname{Attention}(q_{\text{BOS}}, k_{\text{BOS}}, v_{\text{BOS}})
$$
并将其与 cross-attention KV 计算得到：
$$
\operatorname{Attention}
(
q_{\text{cross},0},
K_{\text{cross}}^{(1)},
V_{\text{cross}}^{(1)}
)
$$
输出继续经过 FFN 得到第一层的输出，然后进入第二层 Decoder。第二层与第一层一样，只不过第二层输入不再是 X，而是来自于第一层的输出。然后第三层、第四层......最后输出第一个词：
$$
\text{logits}
\in
\mathbb{R}^{1\times1\times V}
$$
概率可能是：

```
I       0.82
We      0.06
The     0.04
...
```

选择：

```
I
```

然后，对于预测下一个词，现在逻辑上的目标前缀是：

```
<BOS> I
```

但使用 KV Cache 时，不需要重新把 `<BOS>` 完整算一遍。

Decoder 只输入新 token：

```
I
```

当前新 token 产生：
$$
q_I, k_I, v_I
$$
旧缓存是：
$$
\begin{aligned}
K_{\text{cache}} &= [k_{\text{BOS}}] \\
V_{\text{cache}} &= [v_{\text{BOS}}]
\end{aligned}
$$
追加新 K/V：
$$
\begin{aligned}
K_{\text{cache}} &\leftarrow [k_{\text{BOS}}, k_I] \\
V_{\text{cache}} &\leftarrow [v_{\text{BOS}}, v_I]
\end{aligned}
$$
当前 Q 查询历史全部 K，计算得到下一个 cross-attention Q：
$$
q_{cross,1}=\operatorname{Attention}
\left(
q_I,
[k_{\text{BOS}}, k_I],
[v_{\text{BOS}}, v_I]
\right)
$$
后续过程与上述一样，不在赘述。

## 参考

[Attention Is All You Need](https://arxiv.org/abs/1706.03762)

[Tensor2Tensor](https://github.com/tensorflow/tensor2tensor)
