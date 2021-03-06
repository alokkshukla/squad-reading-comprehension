from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import time
import logging
import os
import copy
import random
import sys
import math
from datetime import datetime

import numpy as np
from six.moves import xrange  # pylint: disable=redefined-builtin
import tensorflow as tf
from tensorflow.python.ops import variable_scope as vs

from tensorflow.python.ops.nn import sparse_softmax_cross_entropy_with_logits
from tensorflow.python.ops.nn import bidirectional_dynamic_rnn
from tensorflow.python.ops.nn import dynamic_rnn

from evaluate import exact_match_score, f1_score
from utils import beta_summaries

logging.basicConfig(level=logging.INFO)


def get_optimizer(opt):
    if opt == "adam":
        optfn = tf.train.AdamOptimizer
    elif opt == "sgd":
        optfn = tf.train.GradientDescentOptimizer
    else:
        assert (False)
    return optfn

class MatchLSTMCell(tf.nn.rnn_cell.BasicLSTMCell):
    """
    Extension of LSTM cell to do matching and magic. Designed to be fed to dynammic_rnn
    """
    def __init__(self, hidden_size, HQ, FLAGS):
         # Uniform distribution, as opposed to xavier, which is normal
        self.HQ = HQ
        self.hidden_size = hidden_size
        self.FLAGS = FLAGS

        l, P, Q = self.hidden_size, self.FLAGS.max_paragraph_size, self.FLAGS.max_question_size
        self.WQ = tf.get_variable("WQ", [l,l], initializer=tf.uniform_unit_scaling_initializer(1.0)) 
        self.WP = tf.get_variable("WP", [l,l], initializer=tf.uniform_unit_scaling_initializer(1.0))
        self.WR = tf.get_variable("WR", [l,l], initializer=tf.uniform_unit_scaling_initializer(1.0))

        self.bP = tf.Variable(tf.zeros([1, l]))
        self.w = tf.Variable(tf.zeros([l,1])) 
        self.b = tf.Variable(tf.zeros([1,1]))

        # Calculate term1 by resphapeing to l
        HQ_shaped = tf.reshape(HQ, [-1, l])
        term1 = tf.matmul(HQ_shaped, self.WQ)
        term1 = tf.reshape(term1, [-1, Q, l])
        self.term1 = term1

        super(MatchLSTMCell, self).__init__(hidden_size)

    def __call__(self, inputs, state, scope = None):
        """
        inputs: a batch representation (HP at each word i) that is inputs = hp_i and are [None, l]
        state: a current state for our cell which is LSTM so its a tuple of (c_mem, h_state), both are [None, l]
        """
        
        #For naming convention load in from self the params and rename
        term1 = self.term1
        WQ, WP, WR = self.WQ, self.WP, self.WR
        bP, w, b = self.bP, self.w, self.b
        l, P, Q = self.hidden_size, self.FLAGS.max_paragraph_size, self.FLAGS.max_question_size
        HQ = self.HQ
        hr = state[1]
        hp_i = inputs

        # Check correct input dimensions
        assert hr.get_shape().as_list() == [None, l]
        assert hp_i.get_shape().as_list() == [None, l]

        # Way to extent a [None, l] matrix by dim Q (kinda a hack)
        term2 = tf.matmul(hp_i,WP) + tf.matmul(hr, WR) + bP
        term2 = tf.transpose(tf.stack([term2 for _ in range(Q)]), [1,0,2])

        # Check correct term dimensions for use
        assert term1.get_shape().as_list() == [None, Q, l]
        assert term2.get_shape().as_list() == [None, Q, l]

        # Yeah pretty sure we need this lol
        G_i = tf.tanh(term1 + term2)

        # Reshape to multiply against w
        G_i_shaped = tf.reshape(G_i, [-1, l])
        a_i = tf.matmul(G_i_shaped, w) + b
        a_i = tf.reshape(a_i, [-1, Q, 1])

        # Check that the attention matrix is properly shaped (3rd dim useful for batch_matmul in next step)
        assert a_i.get_shape().as_list() == [None, Q, 1]

        # Prepare dims, and mult attn with question representation in each element of the batch
        HQ_shaped = tf.transpose(HQ, [0,2,1])
        z_comp = tf.batch_matmul(HQ_shaped, a_i)
        z_comp = tf.squeeze(z_comp, [2])

        # Check dims of above operation
        assert z_comp.get_shape().as_list() == [None, l]

        # Concatenate elements for feed into LSTM
        z_i = tf.concat(1,[hp_i, z_comp])

        # Check dims of LSTM input
        assert z_i.get_shape().as_list() == [None, 2*l]

        # Return resultant hr and state from super class (BasicLSTM) run with z_i as input and current state given to our cell
        hr, state = super(MatchLSTMCell, self).__call__(z_i, state)

        return hr, state

class Encoder(object):
    def __init__(self, size, vocab_dim, FLAGS):
        self.size = size
        self.vocab_dim = vocab_dim
        self.FLAGS = FLAGS

    def encode(self, input_question, input_paragraph, question_length, paragraph_length, encoder_state_input = None):    # LSTM Preprocessing and Match-LSTM Layers
        """
        Description:
        """

        assert input_question.get_shape().as_list() == [None, self.FLAGS.max_question_size, self.FLAGS.embedding_size]
        assert input_paragraph.get_shape().as_list() == [None, self.FLAGS.max_paragraph_size, self.FLAGS.embedding_size]

        #Preprocessing LSTM
        with tf.variable_scope("question_encode"):
            cell = tf.nn.rnn_cell.BasicLSTMCell(self.size) #self.size passed in through initialization from "state_size" flag
            HQ, _ = tf.nn.dynamic_rnn(cell, input_question, sequence_length = question_length,  dtype = tf.float32)

        with tf.variable_scope("paragraph_encode"):
            cell2 = tf.nn.rnn_cell.BasicLSTMCell(self.size)
            HP, _ = tf.nn.dynamic_rnn(cell2, input_paragraph, sequence_length = paragraph_length, dtype = tf.float32)   #sequence length masks dynamic_rnn

        assert HQ.get_shape().as_list() == [None, self.FLAGS.max_question_size, self.FLAGS.state_size]
        assert HP.get_shape().as_list() == [None, self.FLAGS.max_paragraph_size, self.FLAGS.state_size]

        # Encoding params
        l = self.size
        Q = self.FLAGS.max_question_size
        P = self.FLAGS.max_paragraph_size

        # Initialize forward and backward matching LSTMcells with same matching params
        with tf.variable_scope("forward"):
            cell_f = MatchLSTMCell(l, HQ, self.FLAGS) 
        with tf.variable_scope("backward"):
            cell_b = MatchLSTMCell(l, HQ, self.FLAGS)

        # Calculate encodings for both forward and backward directions
        (HR_right, HR_left), _ = tf.nn.bidirectional_dynamic_rnn(cell_f, cell_b, HP, sequence_length = paragraph_length, dtype = tf.float32)
        
        ### Append the two things calculated above into H^R
        HR = tf.concat(2,[HR_right, HR_left])
        assert HR.get_shape().as_list() == [None, P, 2*l]
    
        return HR

class Decoder(object):
    def __init__(self, FLAGS):
        self.FLAGS = FLAGS

    def decode(self, knowledge_rep, paragraph_mask, cell_init): 
        """

        :param knowledge_rep: it is a representation of the paragraph and question                
        :return:
        """

        # Decode Params
        l = self.FLAGS.state_size
        P = self.FLAGS.max_paragraph_size
        Hr = knowledge_rep  

        # Decode variables
        V = tf.get_variable("V", [2*l,l], initializer=tf.contrib.layers.xavier_initializer())   
        Wa = tf.get_variable("Wa", [l,l], initializer=tf.contrib.layers.xavier_initializer())
        ba = tf.Variable(tf.zeros([1,l]), name = "ba")
        v = tf.Variable(tf.zeros([l,1]), name = "v")
        c = tf.Variable(tf.zeros([1]), name = "c")
       
        # Basic LSTM for decoding
        cell = tf.nn.rnn_cell.BasicLSTMCell(l)

        # Preds[0] for predictions start span, and Preds[1] for end of span
        preds = [None, None]
        
        # Initial hidden layer (and state) from placeholder
        hk = cell_init
        cell_state = (hk, hk)
        assert hk.get_shape().as_list() == [None, l] 

        # Just two iterations of decoding for the start point and then the end point
        for i, _ in enumerate(preds):  
            if i > 0: #Round 2 should reuse variables from before
                tf.get_variable_scope().reuse_variables()

            # Mult and extend using hack to get shape compatable
            term2 = tf.matmul(hk,Wa) + ba 
            term2 = tf.transpose(tf.stack([term2 for _ in range(P)]), [1,0,2]) 
            assert term2.get_shape().as_list() == [None, P, l] 
            
            # Reshape and matmul
            Hr_shaped = tf.reshape(Hr, [-1, 2*l])
            term1 = tf.matmul(Hr_shaped, V)
            term1 = tf.reshape(term1, [-1, P, l])
            assert term1.get_shape().as_list() == [None, P, l] 

            # Add terms and tanh them
            Fk = tf.tanh(term1 + term2)
            assert Fk.get_shape().as_list() == [None, P, l] 

            # Generate beta_term v^T * Fk + c * e(P)
            Fk_shaped = tf.reshape(Fk, [-1, l])
            beta_term = tf.matmul(Fk_shaped, v) + c
            beta_term = tf.reshape(beta_term ,[-1, P, 1])
            assert beta_term.get_shape().as_list() == [None, P, 1] 

            # Get Beta (prob dist over the paragraph)
            beta = tf.nn.softmax(beta_term)
            assert beta.get_shape().as_list() == [None, P, 1] 

            # Setup input to LSTM
            Hr_shaped_cell = tf.transpose(Hr, [0, 2, 1])
            cell_input = tf.squeeze(tf.batch_matmul(Hr_shaped_cell, beta), [2])
            assert cell_input.get_shape().as_list() == [None, 2*l] 

            # Ouput and State for next iteration
            hk, cell_state = cell(cell_input, cell_state)

            #Save a 2D rep of Beta as output
            preds[i] = tf.squeeze(beta_term)    # TODO: Do we want beta? Or beta_term?   Beta would be softmaxed twice by this

        return tuple(preds) # Bs, Be [batchsize, paragraph_length]


class QASystem(object):
    def __init__(self, encoder, decoder, FLAGS, *args):
        """
        Initializes your System

        :param encoder: an encoder that you constructed in train.py
        :param decoder: a decoder that you constructed in train.py
        :param args: pass in more arguments as needed
        """
        self.encoder = encoder
        self.decoder = decoder
        self.FLAGS = FLAGS

        # ==== set up variables ========
        self.learning_rate = tf.Variable(float(self.FLAGS.learning_rate), trainable = False, name = "learning_rate")
        self.global_step = tf.Variable(int(0), trainable = False, name = "global_step")

        # # ==== set up placeholder tokens ======== 3d (because of batching)
        self.paragraph_placeholder = tf.placeholder(tf.int32, (None, self.FLAGS.max_paragraph_size), name="paragraph_placeholder")
        self.question_placeholder = tf.placeholder(tf.int32, (None, self.FLAGS.max_question_size), name="question_placeholder")
        self.start_answer_placeholder = tf.placeholder(tf.int32, (None), name="start_answer_placeholder")
        self.end_answer_placeholder = tf.placeholder(tf.int32, (None), name="end_answer_placeholder")
        self.paragraph_mask_placeholder = tf.placeholder(tf.bool, (None, self.FLAGS.max_paragraph_size), name="paragraph_mask_placeholder")
        self.paragraph_length = tf.placeholder(tf.int32, (None), name="paragraph_length")
        self.question_length = tf.placeholder(tf.int32, (None), name="question_length")
        self.cell_initial_placeholder = tf.placeholder(tf.float32, (None, self.FLAGS.state_size), name="cell_init")
        #self.dropout_placeholder = tf.placeholder(tf.float32, (), name="dropout_placeholder")

        # ==== assemble pieces ====
        with tf.variable_scope("qa", initializer=tf.uniform_unit_scaling_initializer(1.0)):
            self.setup_embeddings()
            self.setup_system()
            self.setup_loss()
            self.setup_predictions()

        # ==== set up training/updating procedure ==
        opt_function = get_optimizer(self.FLAGS.optimizer)  #Default is Adam
        #self.decayed_rate = tf.train.exponential_decay(self.learning_rate, self.global_step, decay_steps = 1000, decay_rate = 0.95, staircase=True)
        tf.summary.scalar("learning_rate", self.learning_rate)
        optimizer = opt_function(self.learning_rate)

        grads_and_vars = optimizer.compute_gradients(self.loss, tf.trainable_variables())

        grads = [g for g, v in grads_and_vars]
        variables = [v for g, v in grads_and_vars]

        clipped_grads, self.global_norm = tf.clip_by_global_norm(grads, self.FLAGS.max_gradient_norm)
        tf.summary.scalar("global_norm", self.global_norm)
        self.train_op = optimizer.apply_gradients(zip(clipped_grads, variables), global_step = self.global_step, name = "apply_clipped_grads")

        self.saver = tf.train.Saver(tf.global_variables())


    def setup_system(self):
        Hr = self.encoder.encode(self.question_embedding, self.paragraph_embedding, self.question_length, self.paragraph_length)
        self.pred_s, self.pred_e = self.decoder.decode(Hr, self.paragraph_mask_placeholder, self.cell_initial_placeholder)
        

    def setup_predictions(self):
        with vs.variable_scope("prediction"):
            masked_pred_s = tf.boolean_mask(self.pred_s, self.paragraph_mask_placeholder)
            masked_pred_e = tf.boolean_mask(self.pred_e, self.paragraph_mask_placeholder)

            self.Beta_s = tf.nn.softmax(masked_pred_s)
            self.Beta_e = tf.nn.softmax(masked_pred_e)
            beta_summaries(self.Beta_s, "Beta_S")
            beta_summaries(self.Beta_e, "Beta_E")



    def setup_loss(self):
        with vs.variable_scope("loss"):
            start_predictions = tf.unstack(self.pred_s, self.FLAGS.batch_size)
            end_predictions = tf.unstack(self.pred_e, self.FLAGS.batch_size)
            masks = tf.unstack(self.paragraph_mask_placeholder, self.FLAGS.batch_size)

            masked_preds_s = [tf.boolean_mask(p, mask) for p, mask in zip(start_predictions, masks)]
            masked_preds_e = [tf.boolean_mask(p, mask) for p, mask in zip(end_predictions, masks)]

            loss_list_1 = [tf.nn.sparse_softmax_cross_entropy_with_logits(masked_preds_s[i], self.start_answer_placeholder[i]) for i in range(len(masked_preds_s))]
            loss_list_2 = [tf.nn.sparse_softmax_cross_entropy_with_logits(masked_preds_e[i], self.end_answer_placeholder[i]) for i in range(len(masked_preds_e))]
            l1 = tf.reduce_mean(loss_list_1)
            l2 = tf.reduce_mean(loss_list_2)
            self.loss = l1 + l2
            tf.summary.scalar('loss', self.loss)
        

    def setup_embeddings(self):
        """
        Loads distributed word representations based on placeholder tokens
        """
        with vs.variable_scope("embeddings"):
            embed_file = np.load(self.FLAGS.embed_path)
            pretrained_embeddings = embed_file['glove']
            embeddings = tf.Variable(pretrained_embeddings, name = "embeddings", dtype=tf.float32, trainable = False)
            self.paragraph_embedding = tf.nn.embedding_lookup(embeddings,self.paragraph_placeholder)
            self.question_embedding = tf.nn.embedding_lookup(embeddings,self.question_placeholder)


    def decode(self, session, qs, ps, q_masks, p_masks):  #Currently still decodes one at a time
        """
        Returns the probability distribution over different positions in the paragraph
        so that other methods like self.answer() will be able to work properly
        """
        input_feed = {}

        input_feed[self.question_placeholder] = np.array(list(qs))
        input_feed[self.paragraph_placeholder] = np.array(list(ps))
        input_feed[self.paragraph_mask_placeholder] = np.array(list(p_masks))
        input_feed[self.paragraph_length] = np.sum(list(p_masks), axis = 1)   # Sum and make into a list
        input_feed[self.question_length] = np.sum(list(q_masks), axis = 1)    # Sum and make into a list
        input_feed[self.cell_initial_placeholder] = np.zeros((1, self.FLAGS.state_size))

        output_feed = [self.Beta_s, self.Beta_e]    # Get the softmaxed outputs

        outputs = session.run(output_feed, input_feed)

        return outputs


    def answer(self, session, question, paragraph, question_mask, paragraph_mask):

        B_s, B_e = self.decode(session, [question], [paragraph], [question_mask], [paragraph_mask])

        a_s = np.argmax(B_s)
        a_e = np.argmax(B_e)

        #Force a_e to be after a_s.
        if a_e < a_s:
            if np.max(B_s) > np.max(B_e):   #Move a_e to a_s b/c a_s has a higher probability
                a_e = a_s
            else:                           #Move a_s to a_e b/c a_e has a higher probability
                a_s = a_e

        return a_s, a_e


    def evaluate_answer(self, session, dataset, rev_vocab, sample=100, log=False):
        """
        Evaluate the model's performance using the harmonic mean of F1 and Exact Match (EM)
        with the set of true answer labels

        This step actually takes quite some time. So we can only sample 100 examples
        from either training or testing set.

        :param session: session should always be centrally managed in train.py
        :param dataset: a representation of our data, in some implementations, you can
                        pass in multiple components (arguments) of one dataset to this function
        :param sample: how many examples in dataset we look at
        :param log: whether we print to std out stream
        :return:
        """
        
        our_answers = []
        their_answers = []
        for question, question_mask, paragraph, paragraph_mask, span, true_answer in random.sample(dataset, sample):
            a_s, a_e = self.answer(session, question, paragraph, question_mask, paragraph_mask)
            token_answer = paragraph[a_s : a_e + 1]      #The slice of the context paragraph that is our answer

            sentence = []
            for token in token_answer:
                word = rev_vocab[token]
                sentence.append(word)

            our_answer = ' '.join(word for word in sentence)
            our_answers.append(our_answer)
            their_answer = ' '.join(word for word in true_answer)
            their_answers.append(their_answer)

        f1 = exact_match = total = 0
        answer_tuples = zip(their_answers, our_answers)
        for ground_truth, prediction in answer_tuples:
            total += 1
            exact_match += exact_match_score(prediction, ground_truth)
            f1 += f1_score(prediction, ground_truth)

        exact_match = 100.0 * exact_match / total
        f1 = 100.0 * f1 / total

        if log:
            logging.info("F1: {}, EM: {}, for {} samples".format(f1, exact_match, sample))
            logging.info("Samples:")
            for i in xrange(min(10, sample)):
                ground_truth, our_answer = answer_tuples[i]
                logging.info("Ground Truth: {}, Our Answer: {}".format(ground_truth, our_answer))

        return f1, exact_match
    

    def optimize(self, session, batch):
        """
        Takes in actual data to optimize your model
        This method is equivalent to a step() function
        :return:
        """
        train_qs, train_q_masks, train_ps, train_p_masks, train_spans, train_answers = zip(*batch)    # Unzip batch, each returned element is a tuple of lists

        input_feed = {}

        start_answers = [train_span[0] for train_span in list(train_spans)]
        end_answers = [train_span[1] for train_span in list(train_spans)]

        input_feed[self.question_placeholder] = np.array(list(train_qs))
        input_feed[self.paragraph_placeholder] = np.array(list(train_ps))
        input_feed[self.start_answer_placeholder] = np.array(start_answers)
        input_feed[self.end_answer_placeholder] = np.array(end_answers)
        input_feed[self.paragraph_mask_placeholder] = np.array(list(train_p_masks))
        input_feed[self.paragraph_length] = np.sum(list(train_p_masks), axis = 1)   # Sum and make into a list
        input_feed[self.question_length] = np.sum(list(train_q_masks), axis = 1)    # Sum and make into a list
        #input_feed[self.dropout_placeholder] = self.FLAGS.dropout
        input_feed[self.cell_initial_placeholder] = np.zeros((self.FLAGS.batch_size, self.FLAGS.state_size))

        output_feed = []

        output_feed.append(self.train_op)
        output_feed.append(self.loss)
        #output_feed.append(self.decayed_rate)
        output_feed.append(self.global_norm)
        output_feed.append(self.global_step)
        
        if self.FLAGS.tb is True:
            output_feed.append(self.tb_vars)
            tr, loss, norm, step, summary = session.run(output_feed, input_feed)
            self.tensorboard_writer.add_summary(summary, step)
        else:
            tr, loss, norm, step = session.run(output_feed, input_feed) 

        return loss, norm, step

    def get_batch(self, dataset):
        batch = random.sample(dataset, self.FLAGS.batch_size)
        for i, (q, q_mask, p, p_mask, span, answ) in enumerate(batch):
            while span[1] >= 300:    # Simply dont process any questions with answers outside of the possible range
                (q, q_mask, p, p_mask, span, answ) = random.choice(dataset)
                batch[i] = (q, q_mask, p, p_mask, span, answ)
        return batch


    def train(self, session, dataset, train_dir, rev_vocab):
        """
        Implement main training loop

        TIPS:
        You should also implement learning rate annealing (look into tf.train.exponential_decay)
        Considering the long time to train, you should save your model per epoch.

        More ambitious approach can include implement early stopping, or reload
        previous models if they have higher performance than the current one

        As suggested in the document, you should evaluate your training progress by
        printing out information every fixed number of iterations.

        We recommend you evaluate your model performance on F1 and EM instead of just
        looking at the cost.

        :param session: it should be passed in from train.py
        :param dataset: a representation of our data, in some implementations, you can
                        pass in multiple components (arguments) of one dataset to this function
        :param train_dir: path to the directory where you should save the model checkpoint
        :return:
        """
        if self.FLAGS.tb is True:
            tensorboard_path = os.path.join(self.FLAGS.log_dir, "tensorboard")
            self.tb_vars = tf.summary.merge_all()        
            self.tensorboard_writer = tf.summary.FileWriter(tensorboard_path, session.graph)

        tic = time.time()
        params = tf.trainable_variables()
        num_params = sum(map(lambda t: np.prod(tf.shape(t.value()).eval()), params))
        toc = time.time()
        logging.info("Number of params: %d (retreival took %f secs)" % (num_params, toc - tic))

        #Info for saving models
        saver = tf.train.Saver()
        start_time = "{:%d-%m-%Y_%H:%M:%S}".format(datetime.now())
        model_name = "match-lstm"
        checkpoint_path = os.path.join(train_dir, model_name, start_time)
        early_stopping_path = os.path.join(checkpoint_path, "early_stopping")

        train_data = zip(dataset["train_questions"], dataset["train_questions_mask"], dataset["train_context"], dataset["train_context_mask"], dataset["train_span"], dataset["train_answer"])
        dev_data = zip(dataset["val_questions"], dataset["val_questions_mask"], dataset["val_context"], dataset["val_context_mask"], dataset["val_span"], dataset["val_answer"])

        num_data = len(train_data)

        best_f1 = 0

        # Normal training loop
        rolling_ave_window = 20
        losses = [0]*rolling_ave_window
        for cur_epoch in range(self.FLAGS.epochs):
            for i in range(int(math.ceil(num_data/self.FLAGS.batch_size))):
                batch = self.get_batch(train_data)

                loss, norm, step = self.optimize(session, batch)
                losses[step % rolling_ave_window] = loss

                mean_loss = np.mean(losses)
                num_complete = int(20*(self.FLAGS.batch_size*float(i+1)/num_data))
                sys.stdout.write('\r')
                sys.stdout.write("EPOCH: %d ==> (Rolling Ave Loss: %.3f, Batch Loss: %.3f) [%-20s] (Completion:%d/%d) [norm: %.2f]" % (cur_epoch + 1, mean_loss, loss, '='*num_complete, (i+1)*self.FLAGS.batch_size, num_data, norm))
                sys.stdout.flush()

            sys.stdout.write('\n')
            
            logging.info("---------- Evaluating on Train Set ----------")
            self.evaluate_answer(session, train_data, rev_vocab, sample=self.FLAGS.eval_size, log=True)
            logging.info("---------- Evaluating on Dev Set ------------")
            f1, em = self.evaluate_answer(session, dev_data, rev_vocab, sample=self.FLAGS.eval_size, log=True)

            #Save model after each epoch
            if not os.path.exists(checkpoint_path):
                os.makedirs(checkpoint_path)
            save_path = saver.save(session, os.path.join(checkpoint_path, "model.ckpt"), step)
            print("Model checkpoint saved in file: %s" % save_path)

            # Save best model based on F1 (Early Stopping)
            if f1 > best_f1:
                best_f1 = f1
                if not os.path.exists(early_stopping_path):
                    os.makedirs(early_stopping_path)
                save_path = saver.save(session, os.path.join(early_stopping_path, "best_model.ckpt"))
                print("New Best F1 Score: %f !!! Best Model saved in file: %s" % (best_f1, save_path))


